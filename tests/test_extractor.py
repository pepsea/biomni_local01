import json

import pytest

from biomni_hypo.config import Settings
from biomni_hypo.extractor import (
    HypothesisExtractor,
    build_candidates,
    parse_response,
)
from biomni_hypo.fixtures import (
    SAMPLE_QUESTION,
    SAMPLE_SOLUTION,
    FakeLLM,
    fake_extraction_response,
    sample_steps,
)
from biomni_hypo.schemas import ResourceKind, Stance


@pytest.fixture(scope="module")
def steps():
    return sample_steps()


@pytest.fixture(scope="module")
def candidates(steps):
    return build_candidates(steps)


def test_candidates_cover_all_evidence_kinds(candidates):
    kinds = {c.kind for c in candidates}
    assert ResourceKind.LITERATURE in kinds
    assert ResourceKind.DB_RECORD in kinds
    assert ResourceKind.DATASET in kinds
    assert ResourceKind.COMPUTATION in kinds


def test_candidate_ids_are_unique(candidates):
    eids = [c.eid for c in candidates]
    assert len(eids) == len(set(eids))


def test_parse_valid_response(candidates):
    result = parse_response(fake_extraction_response(), candidates)
    assert result.ok
    assert len(result.hypotheses) == 2
    h = result.hypotheses[0]
    assert h.evidence and h.evidence[0].stance == Stance.SUPPORTS
    assert h.test_plan.experiment


def test_unknown_eid_is_dropped_not_trusted(candidates):
    result = parse_response(fake_extraction_response(include_unknown_eid=True), candidates)
    assert "E999" in result.unknown_eids
    assert all(ev.eid != "E999" for h in result.hypotheses for ev in h.evidence)


def test_excerpt_always_comes_from_the_trace(candidates):
    """LLM が抜粋を書いてきても採用しない。トレースの実テキストで上書きする。"""
    payload = {
        "hypotheses": [
            {
                "statement": "x",
                "rationale": "y",
                "confidence": "high",
                "evidence": [
                    {"eid": "E1", "stance": "supports", "why": "z", "excerpt": "完全に捏造された引用文"}
                ],
                "test_plan": {"experiment": "a", "readout": "b"},
            }
        ]
    }
    result = parse_response(json.dumps(payload), candidates)
    ev = result.hypotheses[0].evidence[0]
    assert "捏造" not in ev.excerpt
    by_eid = {c.eid: c for c in candidates}
    assert ev.excerpt == by_eid["E1"].excerpt


def test_hypothesis_with_no_evidence_is_kept(candidates):
    result = parse_response(fake_extraction_response(), candidates)
    assert any(not h.evidence for h in result.hypotheses)


def test_json_in_code_fence_is_accepted(candidates):
    raw = "説明文\n```json\n" + fake_extraction_response() + "\n```\nおわり"
    assert parse_response(raw, candidates).ok


def test_json_with_surrounding_prose_is_accepted(candidates):
    raw = "以下が結果です。\n" + fake_extraction_response() + "\n以上です。"
    assert parse_response(raw, candidates).ok


def test_broken_json_is_reported_not_raised(candidates):
    result = parse_response("これは JSON ではありません", candidates)
    assert not result.ok
    assert result.parse_error


def test_missing_hypotheses_key(candidates):
    result = parse_response('{"foo": 1}', candidates)
    assert not result.ok and "hypotheses" in result.parse_error


def test_extractor_retries_then_succeeds(steps, monkeypatch):
    settings = Settings()
    extractor = HypothesisExtractor(settings, llm=FakeLLM("壊れた出力"))
    responses = iter(["壊れた出力", fake_extraction_response()])
    monkeypatch.setattr(extractor, "_invoke", lambda prompt: next(responses))
    result = extractor.extract(SAMPLE_QUESTION, steps, SAMPLE_SOLUTION, retries=2)
    assert result.ok


def test_prompt_only_offers_real_candidate_ids(steps, candidates):
    extractor = HypothesisExtractor(Settings(), llm=FakeLLM(""))
    prompt = extractor.build_prompt(SAMPLE_QUESTION, SAMPLE_SOLUTION, steps, candidates)
    assert "E1 |" in prompt
    assert "PMID:17529967" in prompt
    assert "上の「使用できる根拠」に載っている ID のみ" in prompt


def test_extractor_uses_injected_llm(steps):
    llm = FakeLLM(fake_extraction_response())
    extractor = HypothesisExtractor(Settings(), llm=llm)
    result = extractor.extract(SAMPLE_QUESTION, steps, SAMPLE_SOLUTION)
    assert result.ok
    assert llm.calls, "注入した LLM が呼ばれていない"


# --------------------------------------------------------- 論点（最終回答の根拠）
# biomni の <solution> は既定では「採点できる短い答え」を返す設計で、
# 結論に至った筋道が残らない（docs/design/18）。ここで組み立てを取り戻す。


def test_reasoning_points_are_extracted(steps):
    result = parse_response(fake_extraction_response(), build_candidates(steps))
    assert len(result.answer_reasoning) == 3
    first = result.answer_reasoning[0]
    assert first.point.endswith("か"), "論点は問いの形で書かせる"
    assert first.finding
    assert first.weight == "decisive"


def test_counter_arguments_are_kept(steps):
    """都合のよい論点だけ残さない。反証は必ず持ち越す。"""
    result = parse_response(fake_extraction_response(), build_candidates(steps))
    refutes = [p for p in result.answer_reasoning if p.stance is Stance.REFUTES]
    assert refutes, "反証の論点が落ちている"
    assert "直接" in refutes[0].finding


def test_a_point_without_evidence_is_still_kept(steps):
    """根拠が無い論点も情報。捨てると「示せなかった」ことが消える。"""
    result = parse_response(fake_extraction_response(), build_candidates(steps))
    empty = [p for p in result.answer_reasoning if not p.evidence]
    assert empty, "根拠なしの論点が捨てられている"


def test_reasoning_evidence_goes_through_the_same_id_check(steps):
    """論点の根拠も、候補にある ID しか使えない。"""
    payload = json.loads(fake_extraction_response())
    payload["reasoning"][0]["evidence"] = [{"eid": "E999", "why": "捏造"}]
    result = parse_response(json.dumps(payload), build_candidates(steps))
    assert "E999" in result.unknown_eids
    assert result.answer_reasoning[0].evidence == [], "未知の ID が根拠として残っている"
    assert result.answer_reasoning[0].point, "論点そのものは残すこと"


def test_unknown_weight_falls_back_to_supporting(steps):
    payload = json.loads(fake_extraction_response())
    payload["reasoning"][0]["weight"] = "とても重要"
    result = parse_response(json.dumps(payload), build_candidates(steps))
    assert result.answer_reasoning[0].weight == "supporting"


def test_points_without_a_question_are_dropped(steps):
    payload = json.loads(fake_extraction_response())
    payload["reasoning"].append({"point": "   ", "finding": "中身だけある"})
    result = parse_response(json.dumps(payload), build_candidates(steps))
    assert all(p.point.strip() for p in result.answer_reasoning)


def test_uncertainties_are_extracted(steps):
    result = parse_response(fake_extraction_response(), build_candidates(steps))
    assert result.answer_uncertainties
    assert "直接結ぶ実験データ" in result.answer_uncertainties[0]


def test_the_prompt_demands_reasoning():
    """プロンプトが論点を必須にしていること（ここが緩むと結論だけに戻る）。"""
    from biomni_hypo.extractor import HYPOTHESIS_JSON_SCHEMA, PROMPT_TEMPLATE

    assert "reasoning" in HYPOTHESIS_JSON_SCHEMA["required"]
    assert "結論だけでは不十分" in PROMPT_TEMPLATE
    assert "refutes" in PROMPT_TEMPLATE
