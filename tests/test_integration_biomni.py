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
    # 絞り込まないと num_ctx=32768 の大半をシステムプロンプトが占める。
    # （import できないモジュールは自動で外れるので、環境によって数値は動く）
    assert full.context_utilization > 0.7, "絞り込みなしが軽くなった。既定を見直せる"


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
