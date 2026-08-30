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


# ---------------------------------------------- プロセス境界を跨ぐときの入力
# Web アプリは子プロセスへ as_spec() の dict を渡す（pydantic モデルのままでは
# 送れない）。coerce_question が dict を受けそこねると str(dict) が質問文になり、
# 構造化入力が丸ごと無駄になる。実測で踏んだ（docs/design/21）。


def test_coerce_accepts_a_spec_dict():
    q = ResearchQuestion(
        text="STAT1 阻害薬の新規対象疾患", organism="ヒト", focus=["STAT1"], max_hypotheses=5
    )
    restored = coerce_question(q.as_spec())
    assert restored.text == q.text
    assert restored.organism == "ヒト"
    assert restored.focus == ["STAT1"]
    assert restored.mode is q.mode
    assert restored.max_hypotheses == 5


def test_a_spec_dict_does_not_become_the_question_text():
    """壊れ方の再現テスト。dict の repr が text に入ってはいけない。"""
    spec = ResearchQuestion(text="STAT1 阻害薬の新規対象疾患", organism="ヒト").as_spec()
    q = coerce_question(spec)
    assert "'mode'" not in q.text
    assert "{" not in q.text


def test_a_round_trip_produces_the_same_prompt():
    q = ResearchQuestion(
        text="STAT1 阻害薬の新規対象疾患",
        organism="ヒト",
        context="自己免疫疾患",
        focus=["STAT1", "JAK1"],
    )
    assert coerce_question(q.as_spec()).to_prompt("en") == q.to_prompt("en")


def test_plain_strings_still_work():
    q = coerce_question("BRCA1 と乳がんの関係")
    assert q.text == "BRCA1 と乳がんの関係"


def test_the_prompt_forbids_importing_tools():
    """ツールは読み込み済み。import を書かせないこと。

    実測: from biomni.tool.database import query_pubmed を繰り返して
    28 ステップを失った（docs/design/38）。
    """
    q = ResearchQuestion.from_text("TNBC で PARP 阻害剤耐性を規定する因子は？")
    for language in ("ja", "en"):
        prompt = q.to_prompt(language)
        assert "import" in prompt, language
        low = prompt.lower()
        assert "do not" in low or "してはいけない" in prompt, language


def test_the_prompt_discourages_printing_whole_results():
    """丸ごと print は文脈を食い潰す。最初から避けさせる。"""
    q = ResearchQuestion.from_text("TNBC で PARP 阻害剤耐性を規定する因子は？")
    for language in ("ja", "en"):
        prompt = q.to_prompt(language)
        assert "print(" in prompt, language
        assert ("丸ごと" in prompt) or ("whole result" in prompt), language


def test_the_prompt_teaches_one_safe_print_form():
    """どの型でも通る形だけを教えること。`print(r[:800])` は辞書で壊れる。

    禁止として出すのは構わない。**勧めて**いなければよい。
    """
    q = ResearchQuestion.from_text("TNBC で PARP 阻害剤耐性を規定する因子は？")
    for language in ("ja", "en"):
        prompt = q.to_prompt(language)
        assert "print(str(r)[:800])" in prompt, language
        for index in _positions(prompt, "print(r[:800])"):
            around = prompt[max(0, index - 60) : index + 60]
            assert ("書かない" in around) or ("Do NOT" in around), (
                f"{language}: 壊れる形を勧めている: {around}"
            )


def _positions(text: str, needle: str) -> list[int]:
    out, start = [], text.find(needle)
    while start != -1:
        out.append(start)
        start = text.find(needle, start + 1)
    return out


def test_the_prompt_asks_for_flat_code():
    """インデントのあるブロックが、いちばん壊れるところ。"""
    q = ResearchQuestion.from_text("TNBC で PARP 阻害剤耐性を規定する因子は？")
    assert "平らに書く" in q.to_prompt("ja")
    assert "FLAT code" in q.to_prompt("en")


def test_the_prompt_forbids_concluding_absence_from_a_failed_call():
    """呼び出しの失敗を「データが無い」の根拠にさせないこと。"""
    q = ResearchQuestion.from_text("TNBC で PARP 阻害剤耐性を規定する因子は？")
    assert "根拠にならない" in q.to_prompt("ja")
    assert "NOT evidence of absence" in q.to_prompt("en")
