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
from biomni_hypo.schemas import ResourceKind
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
