from unittest.mock import MagicMock

import pytest

# 1 つの欠品でテスト全体が止まらないようにする。モジュール先頭で無条件に
# import すると collection でエラーになり、**関係の無いテストまで実行されない**。
# skip なら残りは走る。この非対称性のために、必ず importorskip を先に置く
pytest.importorskip("fastapi", reason="fastapi が無い環境では API テストをスキップ")

from fastapi.testclient import TestClient  # noqa: E402

import backend.app.main as main  # noqa: E402

LOCAL_MODELS = [
    ("qwen3:14b", 9_276_055_800, 40960),
    ("llama3.1:8b", 4_900_000_000, 131072),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.app.store import RunStore

    monkeypatch.setattr(main, "_STORE", RunStore(tmp_path / "runs.sqlite3"))
    monkeypatch.setattr(main, "_model_catalog", None)
    main._running.clear()
    main._subscribers.clear()
    main._seq.clear()
    return TestClient(main.app)


@pytest.fixture
def client_with_ollama(client, monkeypatch):
    """モック Ollama を向いた状態のクライアント。"""
    from biomni_hypo.mock_ollama import MockOllama

    with MockOllama(models=LOCAL_MODELS) as mock:
        settings = main.SETTINGS.model_copy(deep=True)
        settings.ollama_base_url = mock.base_url
        monkeypatch.setattr(main, "SETTINGS", settings)
        monkeypatch.setattr(main, "_model_catalog", None)
        yield client, mock


def test_health(client):
    body = client.get("/api/health").json()
    assert body["api"] == "ok"
    assert body["commercial_mode"] is True
    assert body["policy_version"] >= 1


def test_policy_endpoint_exposes_the_enforced_lists(client):
    body = client.get("/api/policy").json()
    assert body["mode"] == "commercial_only"
    assert "query_kegg" in body["denied_tools"]
    assert "gwas_catalog.pkl" in body["allowed_datasets"]
    assert "qwen3:14b" in body["allowed_models"]


def test_models_endpoint_lists_allowed_models_even_when_not_pulled(client):
    body = client.get("/api/models").json()
    names = {m["name"] for m in body["models"]}
    assert "qwen3:14b" in names
    assert all(m["allowed"] for m in body["models"] if m["name"] == "qwen3:14b")


def test_models_endpoint_reads_local_ollama(client_with_ollama):
    client, _mock = client_with_ollama
    body = client.get("/api/models").json()

    assert body["ollama"]["reachable"] is True
    by_name = {m["name"]: m for m in body["models"]}

    qwen = by_name["qwen3:14b"]
    assert qwen["installed"] and qwen["allowed"] and qwen["recommended"]
    assert qwen["max_context"] == 40960
    assert qwen["size_gb"] == pytest.approx(9.3)

    # 使えないモデルも理由付きで返す
    llama = by_name["llama3.1:8b"]
    assert llama["installed"] and not llama["allowed"]
    assert "MAU" in llama["reason"]

    assert body["selectable"] == ["qwen3:14b"]
    assert body["default"] == "qwen3:14b"


def test_health_reports_selectable_models(client_with_ollama):
    client, _mock = client_with_ollama
    body = client.get("/api/health").json()
    assert body["models"]["selectable"] == ["qwen3:14b"]
    assert body["models"]["blocked"][0]["name"] == "llama3.1:8b"


def test_run_with_denied_model_is_rejected(client_with_ollama):
    client, _mock = client_with_ollama
    r = client.post("/api/runs", json={"question": QUESTION, "model": "llama3.1:8b"})
    assert r.status_code == 422
    assert "model_unavailable" in r.text
    assert "MAU" in r.text


def test_run_with_model_not_pulled_is_rejected_with_a_hint(client_with_ollama):
    client, _mock = client_with_ollama
    r = client.post("/api/runs", json={"question": QUESTION, "model": "qwen3:32b"})
    assert r.status_code == 422
    assert "ollama pull" in r.text


def test_run_without_ollama_is_rejected(client):
    """Ollama 未起動なら、ラン開始前に 422 で止める。"""
    r = client.post("/api/runs", json={"question": QUESTION})
    assert r.status_code == 422
    assert "model_unavailable" in r.text


QUESTION = "TNBC で PARP 阻害剤耐性を規定する因子の候補は？"


def test_run_requires_a_question(client):
    assert client.post("/api/runs", json={"question": ""}).status_code == 422
    assert client.post("/api/runs", json={}).status_code == 422


def test_too_short_question_is_rejected_before_anything_else(client):
    """入力の不備は、モデル解決より先に返す。"""
    r = client.post("/api/runs", json={"question": "がん"})
    assert r.status_code == 422
    assert "invalid_question" in r.text


def test_question_preview_returns_prompt_and_hints(client):
    r = client.post(
        "/api/question/preview",
        json={"text": QUESTION, "organism": "ヒト", "context": "トリプルネガティブ乳がん"},
    ).json()
    assert QUESTION in r["prompt"]
    assert "ヒト" in r["prompt"]
    assert r["can_run"] is True
    assert isinstance(r["hints"], list)


def test_question_preview_flags_blocking_input(client):
    r = client.post("/api/question/preview", json={"text": "がん"}).json()
    assert r["can_run"] is False
    assert any(h["severity"] == "error" for h in r["hints"])


def test_question_preview_warns_about_commercial_gaps(client):
    """商用モードで弱い領域は、実行前に知らせる。"""
    r = client.post(
        "/api/question/preview",
        json={"text": "この疾患で GSEA のエンリッチメント解析から候補経路を挙げてください", "organism": "ヒト"},
    ).json()
    assert any("MSigDB" in h["message"] for h in r["hints"])


def test_question_templates(client):
    body = client.get("/api/question/templates").json()
    assert {m["id"] for m in body["modes"]} == {
        "hypothesis",
        "evidence_check",
        "data_interpretation",
    }
    assert body["templates"] and all(t["text"] for t in body["templates"])


def test_structured_input_is_accepted_and_stored(client_with_ollama, monkeypatch):
    """構造化入力がそのままランに記録されること（実行はモックで止める）。"""
    client, _mock = client_with_ollama
    spawned = {}

    class _Proc:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

        def terminate(self):
            return None

    class _Queue:
        def get(self):
            return {"run_id": "x", "kind": "_eof", "payload": {}}

    def fake_spawn(run_id, question_spec, settings_dict):
        spawned["question_spec"] = question_spec
        spawned["settings"] = settings_dict
        return _Proc(), _Queue()

    monkeypatch.setattr(main, "spawn", fake_spawn)

    r = client.post(
        "/api/runs",
        json={
            "input": {
                "text": QUESTION,
                "mode": "hypothesis",
                "organism": "ヒト",
                "context": "トリプルネガティブ乳がん",
                "focus": ["BRCA1", "BRCA2"],
                "max_hypotheses": 3,
            },
            "model": "qwen3:14b",
        },
    )
    assert r.status_code == 202
    assert spawned["question_spec"]["focus"] == ["BRCA1", "BRCA2"]
    assert spawned["question_spec"]["max_hypotheses"] == 3

    run = client.get(f"/api/runs/{r.json()['run_id']}").json()
    assert run["question_spec"]["organism"] == "ヒト"
    assert "BRCA1" in run["prompt"]


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Biomni 仮説構築" in r.text
    assert "/api/question/preview" in r.text


def test_unknown_run_returns_404(client):
    assert client.get("/api/runs/r_nope").status_code == 404
    assert client.get("/api/runs/r_nope/events").status_code == 404
    assert client.get("/api/runs/r_nope/report").status_code == 404


def test_stored_run_is_served_with_report(client):
    from biomni_hypo.fixtures import sample_steps
    from biomni_hypo.schemas import RunResult

    run = RunResult(id="r_x", question="テスト", status="succeeded", steps=sample_steps())
    main.store().save(run)

    body = client.get("/api/runs/r_x").json()
    assert body["question"] == "テスト"
    assert len(body["steps"]) == len(run.steps)

    md = client.get("/api/runs/r_x/report").text
    assert "# 仮説構築レポート" in md
    assert "## 実行トレース" in md

    listed = client.get("/api/runs").json()["runs"]
    assert [r["id"] for r in listed] == ["r_x"]


def test_events_replay_from_store(client):
    from biomni_hypo.schemas import RunResult

    main.store().save(RunResult(id="r_e", question="q"))
    main.store().append_event("r_e", 1, "status", {"status": "running"})
    main.store().append_event("r_e", 2, "done", {"status": "succeeded"})

    with client.stream("GET", "/api/runs/r_e/events") as r:
        text = "".join(chunk for chunk in r.iter_text())
    assert "event: status" in text
    assert "event: done" in text
    assert "id: 2" in text


def test_health_reports_dependency_status(client):
    """依存が欠けていると子プロセスで初めて落ちる。health で先に見えること。"""
    body = client.get("/api/health").json()
    deps = body["dependencies"]
    assert isinstance(deps["ok"], bool)
    assert isinstance(deps["missing"], list)
    if not deps["ok"]:
        assert deps["install"].startswith("pip install")


def test_providers_endpoint_marks_local_vs_cloud(client):
    body = client.get("/api/providers").json()
    by_name = {p["name"]: p for p in body["providers"]}
    assert by_name["ollama"]["local"] is True
    assert by_name["anthropic"]["local"] is False
    assert by_name["anthropic"]["requires_env"] == "ANTHROPIC_API_KEY"
    assert "外部" in by_name["anthropic"]["note"] or "送信" in by_name["anthropic"]["note"]


def test_models_endpoint_includes_cloud_models(client_with_ollama):
    client, _mock = client_with_ollama
    body = client.get("/api/models").json()
    cloud = [m for m in body["models"] if not m["local"]]
    assert cloud, "クラウドのモデルが一覧に出ていない"
    opus = next(m for m in cloud if m["name"] == "claude-opus-5")
    assert opus["max_context"] == 1000000
    assert opus["input_per_mtok"] == 5.0
    # API キーが無い環境では installed=False + 理由付き
    assert opus["installed"] is False
    assert "ANTHROPIC_API_KEY" in opus["reason"]


def test_index_page_has_answer_and_live_sections(client):
    html = client.get("/").text
    assert 'id="tab-answer"' in html
    assert 'id="tab-sources"' in html
    assert 'addEventListener("token"' in html   # リアルタイム表示
    assert 'id="drawer"' in html                # 根拠ドロワー


def test_providers_readiness_reflects_reality(client_with_ollama):
    """ローカルは Ollama への到達性、クラウドは API キーの有無で判定する。"""
    client, _mock = client_with_ollama
    by_name = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}
    assert by_name["ollama"]["ready"] is True          # モック Ollama に到達できる
    assert by_name["anthropic"]["ready"] is False      # API キー未設定


def test_providers_local_not_ready_when_ollama_is_down(client, monkeypatch):
    settings = main.SETTINGS.model_copy(deep=True)
    settings.ollama_base_url = "http://127.0.0.1:1"
    monkeypatch.setattr(main, "SETTINGS", settings)
    monkeypatch.setattr(main, "_model_catalog", None)
    by_name = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}
    assert by_name["ollama"]["ready"] is False


class _FakeProc:
    """停止テスト用。terminate_tree からは死んだように見える。"""

    def __init__(self):
        self.terminated = False
        self.pid = None

    def is_alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def join(self, timeout=None):
        return None


def test_cancel_marks_the_run_and_frees_the_slot(client, monkeypatch):
    """停止したら状態が cancelled になり、次のランを受け付けられること。"""
    from biomni_hypo.schemas import RunResult

    proc = _FakeProc()
    main.store().save(RunResult(id="r_cancel", question="q", status="running"))
    main._running["r_cancel"] = proc

    r = client.post("/api/runs/r_cancel/cancel")

    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert proc.terminated, "プロセスを止めていない"
    assert main.store().get("r_cancel").status == "cancelled"

    main._running.pop("r_cancel", None)
    main._cancelled.discard("r_cancel")


def test_cancel_unknown_run(client):
    assert client.post("/api/runs/r_nope/cancel").status_code == 404


def test_cannot_delete_a_running_run(client):
    from biomni_hypo.schemas import RunResult

    main.store().save(RunResult(id="r_busy", question="q", status="running"))
    main._running["r_busy"] = _FakeProc()
    try:
        assert client.delete("/api/runs/r_busy").status_code == 409
    finally:
        main._running.pop("r_busy", None)


def test_delete_a_finished_run(client):
    from biomni_hypo.schemas import RunResult

    main.store().save(RunResult(id="r_old", question="q", status="succeeded"))
    assert client.delete("/api/runs/r_old").status_code == 200
    assert client.get("/api/runs/r_old").status_code == 404
    assert client.delete("/api/runs/r_old").status_code == 404


def test_history_page_is_served(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert "調査履歴" in r.text
    assert 'id="hq"' in r.text


def test_the_worker_gets_a_question_it_can_rebuild(client_with_ollama, monkeypatch):
    """API が子プロセスへ渡すものから ResearchQuestion を復元できること。

    ここが壊れると、構造化入力が str(dict) になってエージェントに渡る。
    """
    from biomni_hypo.question import coerce_question

    captured: dict = {}

    def fake_spawn(run_id, question, settings_dict):
        captured["question"] = question
        proc = MagicMock()
        proc.is_alive.return_value = False
        return proc, MagicMock()

    client, _mock = client_with_ollama
    monkeypatch.setattr("backend.app.main.spawn", fake_spawn)
    client.post("/api/runs", json={"input": {
        "text": "STAT1 阻害薬で治療できる新規疾患を探す",
        "mode": "hypothesis", "organism": "ヒト", "focus": ["STAT1"],
    }})
    assert "question" in captured, "spawn が呼ばれていない"
    q = coerce_question(captured["question"])
    assert q.text.startswith("STAT1 阻害薬")
    assert q.organism == "ヒト"
    assert q.focus == ["STAT1"]
    assert "{" not in q.text


# ------------------------------------------------------- 500 が理由を持つこと
# 実測: 「調べる」を押すと画面に `Internal Server Error` の 7 文字だけが出た。
# FastAPI の既定の 500 は本文が空なので、利用者にも開発者にも手掛かりが無い。


def test_unexpected_failure_returns_the_reason(client, monkeypatch):
    """未処理の例外は、型・メッセージ・場所・traceback を JSON で返すこと。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("何かが壊れた")

    monkeypatch.setattr(main, "_catalog", boom)
    # ServerErrorMiddleware は応答を返したうえで例外を再送出する（サーバ側で
    # ログに残せるように）。テストでは受け取る応答のほうを見たいので抑える。
    strict = TestClient(main.app, raise_server_exceptions=False)
    r = strict.post("/api/runs", json={"question": QUESTION, "model": "qwen3:14b"})

    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["error"] == "internal"
    assert "RuntimeError: 何かが壊れた" in detail["detail"]
    assert detail["where"] == "POST /api/runs"
    assert "boom" in detail["traceback"], "traceback を返していない"


def test_store_that_cannot_open_says_why(client, monkeypatch):
    """DB を開けない場合は 503 と、その場で調べた理由を返すこと。"""
    from backend.app.store import StoreUnavailable

    def unavailable():
        raise StoreUnavailable("データベースを開けません: /nowhere/runs.sqlite3\n  権限がありません")

    monkeypatch.setattr(main, "store", unavailable)
    r = client.get("/api/runs")

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["error"] == "store_unavailable"
    assert "データベースを開けません" in detail["detail"]
