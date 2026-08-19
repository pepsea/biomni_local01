import pytest
from fastapi.testclient import TestClient

import backend.app.main as main

LOCAL_MODELS = [
    ("qwen3:14b", 9_276_055_800, 40960),
    ("llama3.1:8b", 4_900_000_000, 131072),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.app.store import RunStore

    monkeypatch.setattr(main, "STORE", RunStore(tmp_path / "runs.sqlite3"))
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
    main.STORE.save(run)

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

    main.STORE.save(RunResult(id="r_e", question="q"))
    main.STORE.append_event("r_e", 1, "status", {"status": "running"})
    main.STORE.append_event("r_e", 2, "done", {"status": "succeeded"})

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
