import pytest

from biomni_hypo.extractor import build_candidates, parse_response
from biomni_hypo.fixtures import fake_extraction_response, sample_steps
from biomni_hypo.schemas import (
    Evidence,
    Hypothesis,
    ResourceKind,
    Step,
    StepKind,
    VerificationStatus,
)
from biomni_hypo.verifier import EvidenceVerifier, TraceIndex


@pytest.fixture(scope="module")
def steps():
    return sample_steps()


@pytest.fixture
def offline_verifier():
    return EvidenceVerifier(offline=True)


def _ev(kind, identifier, step_idx=-1):
    return Evidence(eid="E1", kind=kind, identifier=identifier, step_idx=step_idx)


def test_trace_index_captures_touched_resources(steps):
    idx = TraceIndex.from_steps(steps)
    assert "gwas_catalog.pkl" in idx.datasets_touched
    assert "DepMap_CRISPRGeneEffect.csv" in idx.datasets_touched
    assert idx.executed_steps == {1, 3, 5}


def test_identifier_present_in_observation_passes(steps, offline_verifier):
    idx = TraceIndex.from_steps(steps)
    status, _ = offline_verifier.verify_evidence(_ev(ResourceKind.DB_RECORD, "rs2981582", 2), idx)
    assert status == VerificationStatus.VERIFIED


def test_identifier_absent_from_trace_fails(steps, offline_verifier):
    """C ⊆ B の包含チェック。トレースに無い識別子は無条件で落とす。"""
    idx = TraceIndex.from_steps(steps)
    status, note = offline_verifier.verify_evidence(_ev(ResourceKind.DB_RECORD, "rs99999999", 2), idx)
    assert status == VerificationStatus.FAILED
    assert "幻覚" in note


def test_dataset_not_read_in_code_fails(steps, offline_verifier):
    idx = TraceIndex.from_steps(steps)
    ok, _ = offline_verifier.verify_evidence(_ev(ResourceKind.DATASET, "gwas_catalog.pkl"), idx)
    ng, _ = offline_verifier.verify_evidence(_ev(ResourceKind.DATASET, "proteinatlas.tsv"), idx)
    assert ok == VerificationStatus.VERIFIED
    assert ng == VerificationStatus.FAILED


def test_computation_from_failed_step_is_rejected(offline_verifier):
    steps = [
        Step(idx=0, kind=StepKind.EXECUTE, code="boom()", error="NameError"),
        Step(idx=1, kind=StepKind.EXECUTE, code="print(1)"),
    ]
    idx = TraceIndex.from_steps(steps)
    bad, _ = offline_verifier.verify_evidence(_ev(ResourceKind.COMPUTATION, "step0"), idx)
    good, _ = offline_verifier.verify_evidence(_ev(ResourceKind.COMPUTATION, "step1"), idx)
    assert bad == VerificationStatus.FAILED
    assert good == VerificationStatus.VERIFIED


def test_offline_mode_marks_pmid_not_applicable(steps, offline_verifier):
    idx = TraceIndex.from_steps(steps)
    status, note = offline_verifier.verify_evidence(
        _ev(ResourceKind.LITERATURE, "PMID:17529967", 2), idx
    )
    assert status == VerificationStatus.NOT_APPLICABLE
    assert "オフライン" in note


def test_online_pmid_check_is_injectable(steps):
    calls = []

    def checker(pmid):
        calls.append(pmid)
        return (pmid == "17529967", "確認済" if pmid == "17529967" else "PubMed に存在しない PMID です")

    v = EvidenceVerifier(offline=False, pmid_checker=checker)
    idx = TraceIndex.from_steps(steps)
    ok, _ = v.verify_evidence(_ev(ResourceKind.LITERATURE, "PMID:17529967", 2), idx)
    ng, _ = v.verify_evidence(_ev(ResourceKind.LITERATURE, "PMID:31234567", 6), idx)
    assert ok == VerificationStatus.VERIFIED
    assert ng == VerificationStatus.FAILED
    assert calls == ["17529967", "31234567"]


def test_network_failure_is_not_treated_as_fabrication(steps):
    """ネットワーク障害を「捏造」と誤判定しないこと。"""
    v = EvidenceVerifier(offline=False, pmid_checker=lambda pmid: (None, "確認できず"))
    idx = TraceIndex.from_steps(steps)
    status, _ = v.verify_evidence(_ev(ResourceKind.LITERATURE, "PMID:17529967", 2), idx)
    assert status == VerificationStatus.NOT_APPLICABLE


def test_pmid_result_is_cached(steps):
    calls = []
    v = EvidenceVerifier(offline=False, pmid_checker=lambda p: (calls.append(p), (True, "ok"))[1])
    idx = TraceIndex.from_steps(steps)
    for _ in range(3):
        v.verify_evidence(_ev(ResourceKind.LITERATURE, "PMID:17529967", 2), idx)
    assert calls == ["17529967"]


def test_verify_run_splits_supported_and_unsupported(steps, offline_verifier):
    candidates = build_candidates(steps)
    extraction = parse_response(fake_extraction_response(), candidates)
    supported, unsupported, report = offline_verifier.verify_run(extraction.hypotheses, steps)
    assert len(supported) == 1
    assert len(unsupported) == 1
    assert report.summary.verified + report.summary.not_applicable > 0


def test_failed_evidence_is_removed_from_hypothesis(steps, offline_verifier):
    h = Hypothesis(
        statement="捏造された根拠だけを持つ仮説",
        evidence=[_ev(ResourceKind.DB_RECORD, "rs00000000", 2)],
    )
    supported, unsupported, report = offline_verifier.verify_run([h], steps)
    assert supported == []
    assert unsupported[0].evidence == []
    assert report.failed and report.failed[0].identifier == "rs00000000"


def test_summary_rate_is_computed():
    from biomni_hypo.schemas import VerificationSummary

    assert VerificationSummary(verified=9, failed=1).rate == pytest.approx(0.9)
    assert VerificationSummary().rate == 1.0
