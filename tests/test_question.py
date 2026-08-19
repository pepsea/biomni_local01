"""調べたいことの入力."""

import pytest

from biomni_hypo.question import (
    MODE_LABELS,
    TEMPLATES,
    QuestionMode,
    ResearchQuestion,
    Severity,
    coerce_question,
    normalise_focus,
)

FULL = dict(
    text="トリプルネガティブ乳がんで PARP 阻害剤耐性を規定する因子の候補は？",
    organism="ヒト",
    context="トリプルネガティブ乳がん、オラパリブ投与下",
    focus=["BRCA1", "相同組換え修復"],
    background="BRCA 変異型では奏効する。",
)


def test_minimal_question_only_needs_text():
    q = ResearchQuestion(text="この遺伝子が疾患に寄与する経路は？")
    assert q.mode is QuestionMode.HYPOTHESIS
    assert q.to_prompt()


def test_empty_text_is_rejected():
    with pytest.raises(ValueError):
        ResearchQuestion(text="")


def test_fields_are_stripped_and_cleaned():
    q = ResearchQuestion(text="  テスト用の十分な長さの質問文  ", focus=[" BRCA1 ", "", "  "])
    assert q.text == "テスト用の十分な長さの質問文"
    assert q.focus == ["BRCA1"]


def test_prompt_embeds_every_provided_field():
    prompt = ResearchQuestion(**FULL).to_prompt()
    assert FULL["text"] in prompt
    assert "ヒト" in prompt
    assert "オラパリブ" in prompt
    assert "BRCA1, 相同組換え修復" in prompt
    assert "BRCA 変異型では奏効する。" in prompt


def test_prompt_omits_empty_fields():
    prompt = ResearchQuestion(text="十分な長さのある質問文です", organism="ヒト").to_prompt()
    assert "Organism: ヒト" in prompt
    assert "What is already known" not in prompt


def test_prompt_states_the_evidence_rules():
    """根拠を実データに紐付けさせる指示が必ず入ること。"""
    prompt = ResearchQuestion(**FULL).to_prompt()
    assert "Ground every claim in data you actually retrieved" in prompt
    assert "contradicting evidence" in prompt


@pytest.mark.parametrize("mode", list(QuestionMode))
def test_every_mode_builds_a_prompt_in_both_languages(mode):
    q = ResearchQuestion(**FULL, mode=mode, dataset_ids=["my_deg.csv"])
    assert q.to_prompt("en").strip()
    assert q.to_prompt("ja").strip()
    assert MODE_LABELS[mode]


def test_japanese_prompt_is_japanese():
    prompt = ResearchQuestion(**FULL).to_prompt("ja")
    assert "研究課題:" in prompt and "守ること:" in prompt


def test_data_interpretation_prompt_names_the_files():
    q = ResearchQuestion(
        **FULL, mode=QuestionMode.DATA_INTERPRETATION, dataset_ids=["my_deg.csv"]
    )
    assert "my_deg.csv" in q.to_prompt()


def test_max_hypotheses_reaches_the_prompt():
    assert "up to 3" in ResearchQuestion(**FULL, max_hypotheses=3).to_prompt()


# ------------------------------------------------------------------ 入力検査


def test_short_text_blocks_the_run():
    hints = ResearchQuestion(text="がん").hints()
    assert any(h.severity is Severity.ERROR and h.field == "text" for h in hints)
    assert ResearchQuestion(text="がん").blocking_hints


def test_good_input_has_no_blocking_hints():
    assert ResearchQuestion(**FULL).blocking_hints == []


def test_data_interpretation_requires_data():
    q = ResearchQuestion(**FULL, mode=QuestionMode.DATA_INTERPRETATION)
    assert any(h.field == "dataset_ids" and h.severity is Severity.ERROR for h in q.hints())
    q2 = ResearchQuestion(**FULL, mode=QuestionMode.DATA_INTERPRETATION, dataset_ids=["x.csv"])
    assert q2.blocking_hints == []


def test_missing_organism_is_a_warning_not_an_error():
    q = ResearchQuestion(text="この疾患に関わる経路の候補を挙げてください", context="乳がん")
    hints = [h for h in q.hints() if h.field == "organism"]
    assert hints and hints[0].severity is Severity.WARNING
    assert q.blocking_hints == []


def test_missing_context_and_focus_is_warned():
    q = ResearchQuestion(text="がん化の分子機序について調べてください", organism="ヒト")
    assert any(h.field == "context" for h in q.hints())


def test_too_many_focus_targets_is_warned():
    q = ResearchQuestion(**{**FULL, "focus": [f"GENE{i}" for i in range(20)]})
    assert any(h.field == "focus" for h in q.hints())


# -------------------------------------------- 商用モードで弱い領域の事前警告


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("GSEA でエンリッチメント解析をしたい対象について教えてください", "MSigDB"),
        ("この希少疾患の原因遺伝子の候補を挙げてください", "OMIM"),
        ("miRNA の標的遺伝子について調べてください", "miRTarBase"),
        ("この化合物の結合親和性のデータを集めてください", "BindingDB"),
        ("薬物相互作用のリスクを調べてください", "DDInter"),
    ],
)
def test_excluded_domains_are_flagged_before_running(text, fragment):
    hints = ResearchQuestion(text=text, organism="ヒト", context="乳がん").hints()
    messages = [h.message for h in hints if h.field == "commercial_mode"]
    assert any(fragment in m for m in messages), f"{fragment} の警告が出ていない"
    assert all(h.severity is Severity.INFO for h in hints if h.field == "commercial_mode")


def test_excluded_domain_hints_suggest_alternatives():
    hints = ResearchQuestion(text="GSEA でエンリッチメント解析をしたい", organism="ヒト").hints()
    assert any("代替" in h.message for h in hints if h.field == "commercial_mode")


def test_commercial_hints_can_be_turned_off():
    q = ResearchQuestion(text="miRNA の標的遺伝子を調べたい", organism="ヒト", context="乳がん")
    assert not [h for h in q.hints(commercial_mode=False) if h.field == "commercial_mode"]


# ------------------------------------------------------------------ 補助


def test_templates_are_usable_as_is():
    for t in TEMPLATES:
        q = ResearchQuestion.from_template(t.id)
        assert q.to_prompt()
        assert q.mode.value == t.mode.value


def test_unknown_template():
    with pytest.raises(KeyError):
        ResearchQuestion.from_template("nope")


def test_coerce_accepts_plain_string():
    q = coerce_question("十分な長さのある自由記述の質問です")
    assert isinstance(q, ResearchQuestion)
    assert coerce_question(q) is q


def test_summary_includes_the_setting():
    assert "（乳がん）" in ResearchQuestion(text="経路の候補を挙げてください", context="乳がん").summary


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BRCA1, BRCA2", ["BRCA1", "BRCA2"]),
        ("BRCA1、BRCA2 / TP53", ["BRCA1", "BRCA2", "TP53"]),
        ("", []),
    ],
)
def test_normalise_focus(raw, expected):
    assert normalise_focus(raw) == expected
