import pytest

from biomni_hypo.config import Settings
from biomni_hypo.extractor import HypothesisExtractor
from biomni_hypo.fixtures import (
    TRACE_MESSAGES,
    TRACE_MESSAGES_POLICY,
    FakeA1Module,
    FakeLLM,
    fake_bundle,
    fake_extraction_response,
)
from biomni_hypo.pipeline import collect_resources, run_hypothesis, summarize
from biomni_hypo.report import to_markdown
from biomni_hypo.schemas import ResourceKind, Stance, VerificationStatus
from biomni_hypo.verifier import EvidenceVerifier

QUESTION = "TNBC で PARP 阻害剤耐性を規定する因子は？"


@pytest.fixture
def result(monkeypatch):
    settings = Settings(offline_mode=True)
    bundle = fake_bundle(TRACE_MESSAGES, settings=settings)
    extractor = HypothesisExtractor(settings, llm=FakeLLM(fake_extraction_response()))

    import biomni_hypo.tracing as tracing

    original = tracing.TracingRunner.__init__

    def patched(self, b, run_id=None, *, guard_module=None):
        original(self, b, run_id, guard_module=guard_module or FakeA1Module())

    monkeypatch.setattr(tracing.TracingRunner, "__init__", patched)

    return run_hypothesis(
        QUESTION,
        settings=settings,
        bundle=bundle,
        extractor=extractor,
        verifier=EvidenceVerifier(offline=True),
    )


def test_end_to_end_produces_supported_hypotheses(result):
    assert result.status == "succeeded"
    assert result.steps
    assert result.hypotheses
    assert result.unsupported_ideas  # 根拠ゼロの着想は捨てずに分離される


def test_every_reported_hypothesis_has_evidence(result):
    """受け入れ基準 AC-3。"""
    assert all(h.evidence for h in result.hypotheses)
    assert all(h.is_supported for h in result.hypotheses)


def test_config_is_recorded_for_reproducibility(result):
    c = result.config
    assert c.commercial_mode is True
    assert c.model
    assert c.policy_version >= 1
    assert result.extra["duration_sec"] >= 0


def test_resources_used_carry_licenses(result):
    datasets = [r for r in result.resources_used if r.kind == ResourceKind.DATASET]
    names = {r.name for r in datasets}
    assert "gwas_catalog.pkl" in names
    gwas = next(r for r in datasets if r.name == "gwas_catalog.pkl")
    assert gwas.license == "Apache-2.0"
    assert gwas.attribution
    assert gwas.step_idxs


def test_report_contains_evidence_and_licenses(result):
    md = to_markdown(result)
    assert "## 仮説" in md
    assert "## 使用データとライセンス" in md
    assert "gwas_catalog.pkl" in md
    assert "Apache-2.0" in md
    assert "## 根拠の検証" in md
    assert "## 実行トレース" in md
    assert "```python" in md


def test_report_shows_unsupported_ideas_separately(result):
    md = to_markdown(result)
    assert "## 未裏付けの着想" in md


def test_summary_line(result):
    line = summarize(result)
    assert "仮説" in line and "検証率" in line


def test_collect_resources_marks_review_required():
    from biomni_hypo.policy import ResourcePolicy
    from biomni_hypo.schemas import Step, StepKind, ToolCall

    steps = [
        Step(
            idx=0,
            kind=StepKind.EXECUTE,
            code="pd.read_csv('proteinatlas.tsv')",
            datasets=["proteinatlas.tsv"],
            tools=[ToolCall(name="query_cbioportal", module="biomni.tool.database")],
        )
    ]
    resources = collect_resources(steps, ResourcePolicy.load())
    assert all(r.review_required for r in resources)


def test_report_warns_when_review_required_resources_used():
    from biomni_hypo.policy import ResourcePolicy
    from biomni_hypo.schemas import RunResult, Step, StepKind

    steps = [
        Step(idx=0, kind=StepKind.EXECUTE, code="x", datasets=["proteinatlas.tsv"]),
    ]
    r = RunResult(id="r1", question="q", status="succeeded", steps=steps)
    r.resources_used = collect_resources(steps, ResourcePolicy.load())
    md = to_markdown(r)
    assert "ライセンスの確認が必要" in md


def test_report_flags_hallucinated_run():
    from biomni_hypo.schemas import RunResult

    r = RunResult(id="r1", question="q", status="succeeded")
    r.extra["hallucinated_observations"] = 2
    md = to_markdown(r)
    assert "このランの結果は信用できません" in md


def test_extraction_failure_does_not_fail_the_run(monkeypatch):
    settings = Settings(offline_mode=True)
    bundle = fake_bundle(TRACE_MESSAGES, settings=settings)
    extractor = HypothesisExtractor(settings, llm=FakeLLM("これは JSON ではない"))

    import biomni_hypo.tracing as tracing

    original = tracing.TracingRunner.__init__
    monkeypatch.setattr(
        tracing.TracingRunner,
        "__init__",
        lambda self, b, run_id=None, *, guard_module=None: original(
            self, b, run_id, guard_module=guard_module or FakeA1Module()
        ),
    )

    result = run_hypothesis(
        QUESTION, settings=settings, bundle=bundle, extractor=extractor,
        verifier=EvidenceVerifier(offline=True),
    )
    assert result.status == "succeeded"
    assert result.steps  # トレースは残る
    assert result.extra.get("extraction_error")
    assert result.solution_text  # 自然文の結論は保持される


def test_policy_blocked_run_still_completes(monkeypatch):
    settings = Settings(offline_mode=True)
    bundle = fake_bundle(TRACE_MESSAGES_POLICY, settings=settings)
    extractor = HypothesisExtractor(settings, llm=FakeLLM('{"hypotheses": []}'))

    import biomni_hypo.tracing as tracing

    original = tracing.TracingRunner.__init__
    monkeypatch.setattr(
        tracing.TracingRunner,
        "__init__",
        lambda self, b, run_id=None, *, guard_module=None: original(
            self, b, run_id, guard_module=guard_module or FakeA1Module()
        ),
    )

    result = run_hypothesis(
        QUESTION, settings=settings, bundle=bundle, extractor=extractor,
        verifier=EvidenceVerifier(offline=True),
    )
    md = to_markdown(result)
    assert "ポリシーによりブロック" in md


# ------------------------------------------------- 論点（最終回答に至った根拠）


def test_reasoning_evidence_is_verified_like_everything_else(result):
    """論点だけ検証を免除しない。

    免除すると「もっともらしい筋道」が無検証で通り、根拠モデル全体
    （docs/design/03）に穴が開く。
    """
    assert result.answer_reasoning, "論点が出ていない"
    statuses = {
        e.verification_status
        for p in result.answer_reasoning
        for e in p.evidence
    }
    assert statuses, "論点に根拠が 1 つも紐付いていない"
    assert VerificationStatus.UNVERIFIED not in statuses, "未検証のまま素通りしている"


def test_counter_arguments_survive_the_pipeline(result):
    assert any(p.stance is Stance.REFUTES for p in result.answer_reasoning), "反証が消えた"


def test_uncertainties_survive_the_pipeline(result):
    assert result.answer_uncertainties
    assert not result.extra.get("reasoning_missing")


def test_missing_reasoning_raises_a_flag(monkeypatch):
    """結論だけ返ってきたら黙って出さない（biomni 既定に戻った状態）。"""
    import json

    settings = Settings(offline_mode=True)
    bundle = fake_bundle(TRACE_MESSAGES, settings=settings)
    only_answer = json.dumps(
        {"answer": "FGFR2 が関与する。", "reasoning": [], "hypotheses": []},
        ensure_ascii=False,
    )
    extractor = HypothesisExtractor(settings, llm=FakeLLM(only_answer))

    import biomni_hypo.tracing as tracing

    original = tracing.TracingRunner.__init__
    monkeypatch.setattr(
        tracing.TracingRunner,
        "__init__",
        lambda self, b, run_id=None, *, guard_module=None: original(
            self, b, run_id, guard_module=guard_module or FakeA1Module()
        ),
    )
    r = run_hypothesis(
        QUESTION, settings=settings, bundle=bundle,
        extractor=extractor, verifier=EvidenceVerifier(offline=True),
    )
    assert r.answer
    assert not r.answer_reasoning
    assert r.extra.get("reasoning_missing") is True


# --------------------------------------------------- 論点が無いときの言い分け
# 実測: 「論点を抽出できませんでした」が、どのモデルでも毎回出た。
# 一括りの文言だったため、モデルを替えても直らず原因も分からなかった。


def _run_with_extraction(monkeypatch, payload: dict):
    import json

    import biomni_hypo.tracing as tracing

    settings = Settings(offline_mode=True)
    bundle = fake_bundle(TRACE_MESSAGES, settings=settings)
    extractor = HypothesisExtractor(settings, llm=FakeLLM(json.dumps(payload, ensure_ascii=False)))
    original = tracing.TracingRunner.__init__

    def patched(self, b, run_id=None, *, guard_module=None):
        original(self, b, run_id, guard_module=guard_module or FakeA1Module())

    monkeypatch.setattr(tracing.TracingRunner, "__init__", patched)
    return run_hypothesis(
        QUESTION, settings=settings, bundle=bundle,
        extractor=extractor, verifier=EvidenceVerifier(offline=True),
    )


def test_a_model_that_returned_nothing_is_named_as_such(monkeypatch):
    r = _run_with_extraction(monkeypatch, {"answer": "結論", "hypotheses": []})
    assert r.extra["reasoning_missing"] is True
    assert "返しませんでした" in r.extra["reasoning_missing_reason"]


def test_a_shape_we_could_not_use_is_named_as_such(monkeypatch):
    r = _run_with_extraction(
        monkeypatch,
        {"answer": "結論", "hypotheses": [], "reasoning": [{"nope": 1}, {"also": 2}]},
    )
    assert "2 件" in r.extra["reasoning_missing_reason"]
    assert "形が想定と違う" in r.extra["reasoning_missing_reason"]


def test_a_salvageable_shape_produces_points(monkeypatch):
    """キー名が違うだけの論点は使えること（これが本命の修正）。"""
    r = _run_with_extraction(
        monkeypatch,
        {"answer": "結論", "hypotheses": [],
         "reasoning": [{"question": "BRCA1 の状態が効くか", "observation": "効く"}]},
    )
    assert "reasoning_missing" not in r.extra
    assert [p.point for p in r.answer_reasoning] == ["BRCA1 の状態が効くか"]


# ------------------------------------------ 回答が空のときに理由を必ず言う
# 実測: 「回答が得られませんでした。論点がありません。」だけが表示された。
# パイプラインは理由（例外・打ち切り・タグ無し・ステップ 0）を知っているのに
# 画面に出していなかった。


def _run_with_trace(monkeypatch, messages, *, extraction=None):
    import json

    import biomni_hypo.tracing as tracing

    settings = Settings(offline_mode=True)
    bundle = fake_bundle(messages, settings=settings)
    payload = extraction if extraction is not None else {"answer": "", "hypotheses": []}
    extractor = HypothesisExtractor(settings, llm=FakeLLM(json.dumps(payload, ensure_ascii=False)))
    original = tracing.TracingRunner.__init__

    def patched(self, b, run_id=None, *, guard_module=None):
        original(self, b, run_id, guard_module=guard_module or FakeA1Module())

    monkeypatch.setattr(tracing.TracingRunner, "__init__", patched)
    return run_hypothesis(
        QUESTION, settings=settings, bundle=bundle,
        extractor=extractor, verifier=EvidenceVerifier(offline=True),
    )


NO_SOLUTION = [
    "計画を立てます。まず BRCA1 の状態を確認します。",
    "BRCA1 の変異と PARP 阻害剤感受性の関係を整理すると、相同組換え修復の"
    "欠損が効いている可能性が高いと考えられます。ここまでで結論としては、"
    "HRD の程度が耐性を規定する主因と見てよさそうです。",
]


def test_a_run_without_a_solution_salvages_the_prose(monkeypatch):
    """<solution> が無くても、本文に結論があるなら見せること。"""
    r = _run_with_trace(monkeypatch, NO_SOLUTION)

    assert r.answer, "空白の回答を返している"
    assert "HRD" in r.answer
    assert r.extra["answer_is_unstructured"] is True
    assert r.extra["answer_from"] == "think"


def test_the_reason_is_always_recorded_when_the_answer_is_salvaged(monkeypatch):
    r = _run_with_trace(monkeypatch, NO_SOLUTION)
    reason = r.extra["answer_missing_reason"]
    assert "solution" in reason, reason


def test_the_report_carries_the_reason(monkeypatch):
    r = _run_with_trace(monkeypatch, NO_SOLUTION)
    md = to_markdown(r)
    assert r.extra["answer_missing_reason"] in md


def test_a_trace_with_no_usable_prose_still_says_why(monkeypatch):
    """拾える本文も無い場合でも、理由だけは残すこと。"""
    r = _run_with_trace(monkeypatch, ["はい。"])      # 短すぎて結論とみなさない

    assert not r.answer
    assert r.extra["answer_missing_reason"], "理由が空"
    assert "solution" in r.extra["answer_missing_reason"]


def test_a_normal_run_gets_no_missing_reason(result):
    """普通に回答が出たランに、余計な警告を付けないこと。"""
    assert result.answer
    assert "answer_missing_reason" not in result.extra
    assert "answer_from" not in result.extra


# --------------------------- 「呼び出しが失敗した」を「データが無い」にしない
# 実測: UniProt の失敗は API 障害ではなく slice の書き間違いだったのに、
# 結論は「明確な根拠は得られなかった」だった。読んだ人は
# 「その DB にデータが無い」と受け取る。


BROKEN_CALLS = [
    "調べます。",
    "<execute>print(r[:800])</execute>",
    "<observation>Error: unhashable type: 'slice'</observation>",
    "<execute>print(r['results'])</execute>",
    "<observation>Error: 'results'</observation>",
    "<solution>UniProt へのアクセスが失敗しており、"
    "FGFR1 の機能情報が取得できなかった。明確な根拠は得られなかった。</solution>",
]


def test_client_errors_are_counted_separately(monkeypatch):
    r = _run_with_trace(monkeypatch, BROKEN_CALLS,
                        extraction={"answer": "根拠は得られなかった", "hypotheses": []})
    assert r.extra["client_errors"] >= 2, r.extra


def test_a_gap_claim_built_on_our_own_bugs_is_flagged(monkeypatch):
    r = _run_with_trace(monkeypatch, BROKEN_CALLS,
                        extraction={"answer": "UniProt へのアクセスが失敗しており、"
                                              "データが入手できなかった", "hypotheses": []})
    caveat = r.extra["evidence_gap_caveat"]
    assert "実行したコードの誤り" in caveat
    assert "データが無いことの根拠にはなりません" in caveat


def test_a_normal_answer_is_not_flagged(result):
    """普通に答えが出たランに、この注意書きを付けないこと。"""
    assert "evidence_gap_caveat" not in result.extra


def test_the_report_carries_the_caveat(monkeypatch):
    r = _run_with_trace(monkeypatch, BROKEN_CALLS,
                        extraction={"answer": "データが取得できなかった", "hypotheses": []})
    assert r.extra["evidence_gap_caveat"] in to_markdown(r)


def test_the_report_links_identifiers(result):
    """レポートの識別子は、そのまま踏めること。"""
    md = to_markdown(result)
    linked = [line for line in md.splitlines() if "](http" in line]
    assert linked, "リンクが 1 つも無い"


def test_identifiers_without_a_url_are_left_plain():
    from biomni_hypo.report import _linked
    from biomni_hypo.schemas import Evidence, ResourceKind

    with_url = Evidence(eid="e1", kind=ResourceKind.LITERATURE, identifier="PMID:1",
                        url="https://pubmed.ncbi.nlm.nih.gov/1/")
    without = Evidence(eid="e2", kind=ResourceKind.DB_RECORD, identifier="X-1")
    assert _linked(with_url) == "[PMID:1](https://pubmed.ncbi.nlm.nih.gov/1/)"
    assert _linked(without) == "X-1"
