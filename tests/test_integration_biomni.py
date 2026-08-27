"""実物の biomni + モック Ollama による統合テスト.

`biomni` と `langchain-ollama` が入っていないとスキップする。
本物の Ollama は要らない（モックサーバで代替する）。

ここで守っているのは「設計が実物に対して効いているか」:
  - A1 がデータレイクをダウンロードせずに構築できる（§4.4）
  - 構築直後の agent.llm は stop も num_ctx も持たない = biomni 側の不具合（§4.1）
  - build_agent() 後は stop が実際に HTTP リクエストへ乗る
  - 拒否ツールがシステムプロンプトから消える（§5.2 強制ポイント 2）
  - 本物の実行経路でポリシーガードが割り込む（§5.2 強制ポイント 3）
"""

from __future__ import annotations

import tempfile

import pytest

pytest.importorskip("biomni", reason="biomni 未インストール")
pytest.importorskip("langchain_ollama", reason="langchain-ollama 未インストール")

from biomni_hypo.agent_factory import (  # noqa: E402
    CORE_TOOL_MODULES,
    EXTENDED_TOOL_MODULES,
    build_agent,
)
from biomni_hypo.config import Settings  # noqa: E402
from biomni_hypo.llm import AGENT_STOP_SEQUENCES, build_chat_ollama  # noqa: E402
from biomni_hypo.mock_ollama import MockOllama  # noqa: E402
from biomni_hypo.policy import ResourcePolicy  # noqa: E402
from biomni_hypo.schemas import StepKind  # noqa: E402
from biomni_hypo.tracing import TracingRunner  # noqa: E402


@pytest.fixture(scope="module")
def policy():
    return ResourcePolicy.load()


def _settings(mock: MockOllama, **over) -> Settings:
    s = Settings()
    s.data_path = tempfile.mkdtemp()
    s.ollama_base_url = mock.base_url
    s.use_tool_retriever = False
    for k, v in over.items():
        setattr(s, k, v)
    return s


# --------------------------------------------------------------- LLM の配線


def test_our_builder_sends_stop_and_num_ctx():
    with MockOllama(replies=["ok"]) as mock:
        s = _settings(mock, num_ctx=12345)
        build_chat_ollama(s, stop=AGENT_STOP_SEQUENCES).invoke([{"role": "user", "content": "hi"}])
        options = mock.last_options()
    assert options["stop"] == AGENT_STOP_SEQUENCES
    assert options["num_ctx"] == 12345


def test_biomni_get_llm_drops_stop_sequences(monkeypatch):
    """docs/design/04 §4.1 の不具合が実在することを固定する。

    ここが失敗するようになったら biomni 側が直ったということ。
    その時は agent_factory の差し替えを見直してよい。
    """
    from biomni.llm import get_llm

    with MockOllama(replies=["ok"]) as mock:
        monkeypatch.setenv("OLLAMA_HOST", mock.base_url)  # base_url が効かないため（§4.2）
        llm = get_llm(
            "qwen3:14b", source="Ollama", stop_sequences=AGENT_STOP_SEQUENCES, base_url=mock.base_url
        )
        llm.invoke([{"role": "user", "content": "hi"}])
        options = mock.last_options()

    assert "stop" not in options, "biomni の Ollama 分岐が stop を送るようになった"
    assert "num_ctx" not in options
    assert getattr(llm, "base_url", None) is None, "biomni の Ollama 分岐が base_url を受け取るようになった"


# ------------------------------------------------------------- A1 の構築


def test_a1_builds_without_downloading_the_data_lake(policy):
    """expected_data_lake_files を渡さないと数十 GB を取りに行く（§4.4）。"""
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)
    assert bundle.tool_count > 0
    assert bundle.biomni_version


def test_build_agent_installs_stop_sequences(policy):
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)
    assert getattr(bundle.agent.llm, "stop", None) == AGENT_STOP_SEQUENCES
    assert getattr(bundle.agent.llm, "num_ctx", None) == bundle.settings.num_ctx
    assert getattr(bundle.agent.llm, "base_url", None) == bundle.settings.ollama_base_url


def test_denied_tools_disappear_from_the_system_prompt(policy):
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=EXTENDED_TOOL_MODULES)
    denied = policy.denied_tool_names()
    assert "query_kegg" in denied
    assert bundle.removed_tools
    remaining = {a["name"] for apis in bundle.agent.module2api.values() for a in apis}
    assert not (set(denied) & remaining)
    for name in denied:
        assert name not in bundle.agent.system_prompt


def test_module_presets_control_the_prompt_size(policy):
    """絞り込まないとシステムプロンプトだけで num_ctx を溢れさせる（§4.5）。"""
    with MockOllama() as mock:
        core = build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)
    with MockOllama() as mock:
        full = build_agent(_settings(mock), policy, tool_modules=None)

    assert core.system_prompt_chars < full.system_prompt_chars
    assert core.context_utilization < 0.4, "CORE でも context の 4 割を超えている"
    # 絞り込まないと num_ctx=32768 の大きな割合をシステムプロンプトが占める。
    #
    # 絶対値で固定しない。数値は「どの optional パッケージが入っているか」で動く:
    # 依存が足りないツールは自動で外れる（モジュール単位と関数内 import の両方）。
    # 実測（この環境）: 絞り込みなし 110 tools / 74,492 chars / 57%
    #                   CORE          47 tools / 38,129 chars / 29%
    # scipy や rdkit を入れた環境ではもっと増える。比で見るのが正しい。
    # num_ctx はモデルの上限まで自動で上がるので（§22）、占有率は下がる。
    # 絶対値ではなく「絞り込みなしは CORE より明確に重い」で見る
    assert full.context_utilization > 0.4, "絞り込みなしが軽くなった。既定を見直せる"
    assert full.context_utilization > core.context_utilization * 1.5


def test_unimportable_modules_are_dropped(policy):
    """依存が無いモジュールのツールをエージェントに案内しない。

    案内すると「ツールを呼ぶ -> ImportError -> 直そうとする」のループに陥る
    （実運用で観測した最頻の失敗モード）。
    """
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=None)

    for module in bundle.unusable_modules:
        assert module not in bundle.agent.module2api
    # 除外したモジュールのツールがシステムプロンプトに残っていないこと
    assert bundle.agent.module2api, "すべてのモジュールが外れてしまった"
    for module, package in bundle.unusable_modules.items():
        assert package, f"{module} の不足パッケージ名が空"


def test_literature_and_database_tools_are_available(policy):
    """query_pubmed / query_gwas_catalog が実際に案内されること。

    requirements.txt の biopython / beautifulsoup4 / PyPDF2 /
    googlesearch-python が効いているかの確認。
    """
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)

    names = {a["name"] for apis in bundle.agent.module2api.values() for a in apis}
    assert "query_pubmed" in names, "literature モジュールが import できていない"
    assert "query_gwas_catalog" in names, "database モジュールが import できていない"


# ------------------------------------------------- 本物の ReAct ループ


REPLIES = [
    "まず出力します。\n<execute>\nprint('PMID: 17529967 rs2981582 FGFR2')\n</execute>",
    "次に KEGG を引きます。\n<execute>\nfrom biomni.tool.database import query_kegg\n"
    "print(query_kegg('hsa04110'))\n</execute>",
    "<solution>FGFR2 が候補です。</solution>",
]


@pytest.fixture(scope="module")
def traced(policy):
    with MockOllama(replies=REPLIES) as mock:
        bundle = build_agent(
            _settings(mock, max_steps=20), policy, tool_modules=CORE_TOOL_MODULES
        )
        runner = TracingRunner(bundle, run_id="itest")
        result = runner.run("テスト質問")
        yield result, mock


def test_real_graph_produces_classified_steps(traced):
    result, _ = traced
    kinds = [s.kind for s in result.steps]
    assert kinds.count(StepKind.EXECUTE) == 2
    assert StepKind.OBSERVATION in kinds
    assert kinds[-1] == StepKind.SOLUTION
    assert result.solution_text == "FGFR2 が候補です。"


def test_citations_come_from_real_execution_output(traced):
    result, _ = traced
    obs = next(s for s in result.steps if s.kind == StepKind.OBSERVATION)
    identifiers = {c.identifier for c in obs.citations}
    assert {"PMID:17529967", "rs2981582"} <= identifiers


def test_policy_guard_intercepts_the_real_execution_path(traced):
    """本物の run_python_repl に届く前に止まること（最後の砦）。"""
    result, _ = traced
    blocked = [s for s in result.steps if s.kind == StepKind.POLICY_BLOCKED]
    assert blocked, "query_kegg が実行されてしまった"
    assert "query_kegg" in blocked[0].text


def test_stop_sequences_reach_every_request(traced):
    _, mock = traced
    assert len(mock.chat_requests) == len(REPLIES)
    for req in mock.chat_requests:
        assert req["body"]["options"]["stop"] == AGENT_STOP_SEQUENCES


def test_no_hallucinated_observations(traced):
    result, _ = traced
    assert result.hallucinated_observations == 0


# ------------------------------------------------- パイプライン全体


def test_full_pipeline_against_mock_ollama(policy):
    """run_hypothesis() を実物の A1 + モック Ollama で通す。

    Web ワーカーが呼ぶのと同じ関数なので、ここが通れば API 経由でも通る。
    """
    import json

    from biomni_hypo.pipeline import run_hypothesis

    extraction = json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "FGFR2 の発現上昇が PARP 阻害剤耐性に寄与する",
                    "rationale": "GWAS の関連が観察された。",
                    "confidence": "medium",
                    "novelty": "emerging",
                    "evidence": [
                        {"eid": "E1", "stance": "supports", "claim_span": "FGFR2", "why": "GWAS の関連"},
                        {"eid": "E404", "stance": "supports", "claim_span": "捏造", "why": "存在しない"},
                    ],
                    "assumptions": ["リスク変異が発現量に反映される"],
                    "test_plan": {
                        "experiment": "CRISPRi ノックダウン",
                        "readout": "IC50",
                        "controls": ["非標的 sgRNA"],
                        "feasibility": "high",
                        "estimated_effort": "3 週間",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )

    events: list[str] = []
    with MockOllama(replies=[*REPLIES, extraction]) as mock:
        settings = _settings(mock, max_steps=20, offline_mode=True)
        bundle = build_agent(settings, policy, tool_modules=CORE_TOOL_MODULES)
        result = run_hypothesis(
            "TNBC の PARP 阻害剤耐性は？",
            settings=settings,
            policy=policy,
            bundle=bundle,
            on_event=lambda kind, payload: events.append(kind),
        )

    assert result.status == "succeeded"
    assert result.hypotheses, "仮説が 1 件も残らなかった"

    # 存在しない根拠 ID は破棄される
    assert "E404" in result.extra.get("unknown_eids", [])
    assert all(ev.eid != "E404" for h in result.hypotheses for ev in h.evidence)

    # 使用リソースにライセンスが付く
    assert result.config.commercial_mode is True
    assert result.config.biomni_version

    # SSE に流すイベントが出ている
    assert "phase" in events and "step" in events and "done" in events

    # レポートが生成できる
    from biomni_hypo.report import to_markdown

    md = to_markdown(result)
    assert "# 仮説構築レポート" in md
    assert "ポリシーによりブロック" in md


# ------------------------------------------------- リアルタイム出力


def test_tokens_stream_in_real_time_through_the_real_graph(policy):
    """A1 は invoke() を同期で呼ぶが、ChatOllama は内部でストリーミングしている。

    biomni を改変せずにトークン単位の実況が取れることを固定する。
    """
    replies = [
        "GWAS を調べます。\n<execute>\nprint('PMID: 17529967')\n</execute>",
        "<solution>結論です。</solution>",
    ]
    events: list[tuple[str, dict]] = []
    with MockOllama(replies=replies) as mock:
        bundle = build_agent(
            _settings(mock, max_steps=20), policy, tool_modules=CORE_TOOL_MODULES
        )
        assert bundle.token_stream is not None
        runner = TracingRunner(bundle, run_id="stream")
        result = runner.run("質問", on_event=lambda k, p: events.append((k, p)))

    tokens = [p for k, p in events if k == "token"]
    assert [p["kind"] for p in tokens].count("start") == len(replies)
    assert [p["kind"] for p in tokens].count("end") == len(replies)

    streamed = "".join(p["text"] for p in tokens if p["kind"] == "token")
    assert "GWAS" in streamed and "solution" in streamed
    assert result.streamed_tokens > 0

    # トークンはステップより先に届く（実況になっている）
    kinds = [k for k, _ in events]
    assert kinds.index("token") < kinds.index("step")


def test_token_sink_is_detached_after_the_run(policy):
    """ランの外では実況しない（リソース検索など内部呼び出しまで流さない）。"""
    with MockOllama(replies=["<solution>done</solution>"]) as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)
        TracingRunner(bundle, run_id="detach").run("質問", on_event=lambda k, p: None)
        assert bundle.token_stream.sink is None


def test_tools_with_missing_lazy_imports_are_dropped(policy):
    """関数の中で import するツールも、依存が無ければ案内しない。

    モジュール単位の検査は素通りしてしまうため、呼ばれた瞬間に
    ModuleNotFoundError になる。実測ではそこからエージェントが
    自分の記憶で書き始めた（docs/design/20）。
    """
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=None)

    offered = {a["name"] for apis in bundle.agent.module2api.values() for a in apis}
    for name in bundle.unusable_tools:
        assert name not in offered, f"{name} は呼べないのに案内されている"
        assert name not in bundle.agent.system_prompt


def test_pubmed_survives_when_pymed_is_installed(policy):
    """query_pubmed は残ること。

    文献を引けないと、エージェントは根拠を集められず自分の記憶に頼る。
    このアプリの前提（根拠を示す）が崩れるので、ここは落としてはいけない。
    """
    pytest.importorskip("pymed")
    with MockOllama() as mock:
        bundle = build_agent(_settings(mock), policy, tool_modules=None)

    offered = {a["name"] for apis in bundle.agent.module2api.values() for a in apis}
    assert "query_pubmed" in offered
    assert "query_pubmed" not in bundle.unusable_tools


def test_the_temperature_patch_reaches_the_database_tool(policy):
    """biomni の DB ツールが Claude に temperature を送らないこと。

    database.py は `from biomni.llm import get_llm` をモジュール先頭で行い、
    A1 の構築時に既に import 済みになる。biomni.llm を差し替えるだけでは
    database.py が握っている古い参照は直らないので、そちらも差し替える。

    実測で踏んだ失敗:
      {'success': False, 'error': "... 400 ... '`temperature` is deprecated
       for this model.'"}
    """
    import sys

    with MockOllama() as mock:
        build_agent(_settings(mock), policy, tool_modules=CORE_TOOL_MODULES)

    import biomni.llm as biomni_llm

    assert getattr(biomni_llm.get_llm, "_hypo_patched", False)
    database = sys.modules.get("biomni.tool.database")
    assert database is not None, "database.py が import されていない"
    assert database.get_llm is biomni_llm.get_llm, "古い参照が残っている"

    # 名前を並べず、握っているモジュールを実際に探して差し替えているので、
    # import 済みのものに漏れが無いこと（biomni の更新で増えても効く）
    stale = [
        name
        for name, module in sys.modules.items()
        if module is not None
        and name.startswith("biomni.")
        and getattr(module, "get_llm", None) is not None
        and not getattr(module.get_llm, "_hypo_patched", False)
    ]
    assert not stale, f"古い get_llm を握ったままのモジュール: {stale}"


def test_the_anthropic_client_really_has_no_temperature(policy, monkeypatch):
    """**本物の** get_llm を通して、出来上がったクライアントを調べる。

    ここを stub で検証してはいけない。biomni の get_llm は

        if temperature is None: temperature = config.temperature   # 0.7
        if temperature is None: temperature = 0.7

    と自分で埋め直す（biomni/llm.py:38,52）。kwargs から temperature を
    消すだけの実装は、この既定値の埋め直しで 0.7 が入り、400 が消えない。
    stub を相手にすると「kwargs に無い」ことしか確かめられず、素通しする。
    """
    pytest.importorskip("langchain_anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    import biomni.llm as biomni_llm
    from biomni.config import default_config

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    settings = Settings()
    settings.anthropic_api_key = "sk-ant-test"
    patch_biomni_get_llm(settings, policy)

    # database.py::_query_llm_for_api とまったく同じ呼び方
    for kwargs in (
        {"model": "claude-opus-5", "temperature": 0.0, "api_key": "sk-ant-test"},
        {"model": "claude-opus-5", "temperature": 0.0, "config": default_config},
        {"model": "claude-3-5-sonnet-20241022", "temperature": 0.0},
    ):
        llm = biomni_llm.get_llm(**kwargs)
        assert type(llm).__name__ == "ChatAnthropic"
        assert llm.temperature is None, (
            f"{kwargs['model']} に temperature={llm.temperature} が入っている。"
            "400 になる"
        )


def test_ollama_still_gets_its_temperature(policy, monkeypatch):
    """Anthropic 以外は素通しすること（決定性を落とさない）。"""
    import biomni.llm as biomni_llm

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    patch_biomni_get_llm(Settings(), policy)
    llm = biomni_llm.get_llm(model="qwen3:14b", temperature=0.7, source="Ollama")
    assert type(llm).__name__ == "ChatOllama"
    assert llm.temperature == 0.7


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"model": "claude-opus-5"}, "Anthropic"),
        ({"model": "qwen3:14b", "source": "Ollama"}, "Ollama"),
        ({"model": "qwen3:14b"}, ""),
        ({}, "Anthropic"),                       # biomni の既定は claude-3-5-sonnet
    ],
)
def test_source_resolution_matches_biomni(kwargs, expected):
    """model / source の決め方が biomni とずれていないこと。

    ずれると Anthropic なのに素通しして 400 に戻る。
    """
    from biomni_hypo.agent_factory import _resolve_model_and_source

    _model, source = _resolve_model_and_source((), dict(kwargs))
    assert source == expected


def test_the_patch_picks_up_new_settings(policy, monkeypatch):
    """パッチは 1 プロセス 1 回しか当たらないが、settings は毎回更新すること。

    クロージャに閉じ込めると、最初のランの settings が居座る。
    プロバイダやキーを変えた 2 回目以降のランで、古いキーを使う。
    """
    pytest.importorskip("langchain_anthropic")
    import biomni.llm as biomni_llm

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    first = Settings()
    first.anthropic_api_key = "sk-ant-one"
    patch_biomni_get_llm(first, policy)
    llm = biomni_llm.get_llm(model="claude-opus-5", temperature=0.0)
    assert llm.anthropic_api_key.get_secret_value() == "sk-ant-one"

    second = Settings()
    second.anthropic_api_key = "sk-ant-two"
    patch_biomni_get_llm(second, policy)          # 既にパッチ済みでも
    llm = biomni_llm.get_llm(model="claude-opus-5", temperature=0.0)
    assert llm.anthropic_api_key.get_secret_value() == "sk-ant-two", "古い settings が居座っている"
    assert llm.temperature is None


def test_db_tools_can_use_a_stronger_model(policy, monkeypatch):
    """DB ツールの URL 生成だけ別モデルに寄せられること。

    実測: スキーマに cc_function と書いてあるのに、ローカルモデルが
    function と書いて UniProt に 400 で弾かれた（docs/design/24）。
    エージェント本体はローカルのまま、この 1 か所だけ強いモデルにする。
    """
    pytest.importorskip("langchain_anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    import biomni.llm as biomni_llm

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    settings = Settings()
    settings.model = "qwen3:14b"
    settings.provider = "ollama"
    settings.anthropic_api_key = "sk-ant-test"
    settings.tool_query_model = "claude-sonnet-5"
    patch_biomni_get_llm(settings, policy)

    llm = biomni_llm.get_llm(model="qwen3:14b", temperature=0.0)
    assert type(llm).__name__ == "ChatAnthropic"
    assert llm.model == "claude-sonnet-5"
    assert llm.temperature is None


def test_db_tools_stay_local_by_default(policy):
    """設定しなければ、エージェントと同じモデルのまま（勝手に外へ出さない）。"""
    import biomni.llm as biomni_llm

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    settings = Settings()
    settings.model = "qwen3:14b"
    settings.provider = "ollama"
    patch_biomni_get_llm(settings, policy)

    llm = biomni_llm.get_llm(model="qwen3:14b", temperature=0.0)
    assert type(llm).__name__ == "ChatOllama"


def test_offline_mode_overrides_the_tool_query_model(policy, monkeypatch):
    """オフラインの約束が最優先。設定されていても外へ出さない。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    import biomni.llm as biomni_llm

    from biomni_hypo.agent_factory import patch_biomni_get_llm

    settings = Settings()
    settings.model = "qwen3:14b"
    settings.provider = "ollama"
    settings.anthropic_api_key = "sk-ant-test"
    settings.tool_query_model = "claude-sonnet-5"
    settings.offline_mode = True
    assert settings.tool_query_model_name == "qwen3:14b"

    patch_biomni_get_llm(settings, policy)
    llm = biomni_llm.get_llm(model="qwen3:14b", temperature=0.0)
    assert type(llm).__name__ == "ChatOllama"
