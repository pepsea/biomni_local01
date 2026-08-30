"""毎ターンの出力形式の念押し（docs/design/22）.

タグの規定はシステムプロンプト＝会話の先頭にしか無い。ReAct の手数が増える
ほど生成位置から遠ざかり、num_ctx を超えると古い側から落ちて規定そのものが
消える。結果、biomni に「タグが無い」と差し戻される。

毎ターン、会話の最後尾に短い念押しを置いて、規定を生成の直前に保つ。
"""

import pytest

# langchain はエージェントを動かすときだけ要る。最小構成
# （bash scripts/setup_local.sh）でも pytest が通るよう、
# **モジュール先頭で必須依存にしない**。ここで import すると collection の
# 時点で ModuleNotFoundError になり、テスト全体が止まる（実測で踏んだ）。
pytest.importorskip("langchain_core", reason="langchain-core が無い環境ではスキップ")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
)

from biomni_hypo.llm import TURN_REMINDER, FormatReminderLLM  # noqa: E402


class Recorder:
    """LLM のふり。渡された messages を覚える。"""

    model_name = "recorder"

    def __init__(self) -> None:
        self.seen: list = []

    def invoke(self, messages, *args, **kwargs):
        self.seen = messages
        return AIMessage(content="ok")

    def with_structured_output(self, *_a, **_k):
        return "structured"


@pytest.fixture
def wrapped():
    inner = Recorder()
    return inner, FormatReminderLLM(inner)


def _conversation():
    return [
        SystemMessage(content="システムプロンプト（タグの規定を含む）"),
        HumanMessage(content="研究課題"),
        AIMessage(content="<execute>print(1)</execute>"),
        HumanMessage(content="<observation>1</observation>"),
    ]


def test_the_reminder_lands_at_the_very_end(wrapped):
    inner, llm = wrapped
    llm.invoke(_conversation())
    assert TURN_REMINDER in inner.seen[-1].content
    assert inner.seen[-1].content.startswith("<observation>")


def test_the_message_count_does_not_grow(wrapped):
    """新しいメッセージを積むと human が 2 連続になり、
    チャットテンプレートが崩れるモデルがある。最後のものに足す。"""
    inner, llm = wrapped
    msgs = _conversation()
    llm.invoke(msgs)
    assert len(inner.seen) == len(msgs)


def test_the_caller_s_list_is_not_mutated(wrapped):
    """念押しが履歴に積み上がると、それ自体が context を食う。"""
    inner, llm = wrapped
    msgs = _conversation()
    original = msgs[-1].content
    llm.invoke(msgs)
    assert msgs[-1].content == original


def test_the_reminder_is_not_added_twice(wrapped):
    inner, llm = wrapped
    llm.invoke(_conversation())
    llm.invoke(inner.seen)
    assert inner.seen[-1].content.count("[format]") == 1


def test_attributes_are_delegated(wrapped):
    """biomni は .model_name と .with_structured_output() も使う。"""
    _inner, llm = wrapped
    assert llm.model_name == "recorder"
    assert llm.with_structured_output(dict) == "structured"


def test_non_string_content_is_left_alone(wrapped):
    """マルチモーダルのメッセージを壊さない。"""
    inner, llm = wrapped
    msgs = [HumanMessage(content=[{"type": "text", "text": "hi"}])]
    llm.invoke(msgs)
    assert inner.seen[-1].content == [{"type": "text", "text": "hi"}]


@pytest.mark.parametrize("messages", [[], "文字列", None])
def test_odd_inputs_are_passed_through_untouched(wrapped, messages):
    """list でないもの・空のものは触らない（呼び出し側の想定を壊さない）。"""
    inner, llm = wrapped
    llm.invoke(messages)
    assert inner.seen is messages


def test_the_reminder_names_both_tags():
    assert "<execute>" in TURN_REMINDER
    assert "<solution>" in TURN_REMINDER
    assert "<observation>" in TURN_REMINDER, "自己生成の抑止も入れる（§4.1）"
    assert len(TURN_REMINDER) < 400, "毎ターン払うので短く保つ"


def test_the_agent_llm_is_wrapped():
    """build_agent_llm が包んだものを返すこと。"""
    from biomni_hypo.config import Settings
    from biomni_hypo.llm import build_agent_llm

    llm, _handler = build_agent_llm(Settings())
    assert isinstance(llm, FormatReminderLLM)


def test_the_reminder_never_lands_inside_an_assistant_message(wrapped):
    """最後が assistant のときは、別の human メッセージとして積む。

    assistant のメッセージに足すと、モデルは**自分が書いた文章の続き**として
    読む。指示として効かないどころか、指示文ごと真似される。
    """
    inner, llm = wrapped
    llm.invoke([HumanMessage(content="Q"), AIMessage(content="考え中")])
    assert inner.seen[-1].type == "human"
    assert TURN_REMINDER in inner.seen[-1].content
    assert inner.seen[-2].content == "考え中", "assistant の中身は変えない"


def test_appending_to_a_human_message_does_not_add_a_turn(wrapped):
    inner, llm = wrapped
    msgs = [HumanMessage(content="<observation>1</observation>")]
    llm.invoke(msgs)
    assert len(inner.seen) == 1
    assert inner.seen[-1].type == "human"


# ------------------------------------------------------- 探索の深さの押し戻し
# 小さいモデルは早く満足する。実測では Claude が 6 手かけるところを
# qwen3:14b は 3 手で <solution> を書いた（docs/design/25）。


def _with_executes(n: int) -> list:
    msgs = [SystemMessage(content="SYS"), HumanMessage(content="Q")]
    for i in range(n):
        msgs.append(AIMessage(content=f"<execute>print({i})</execute>"))
        msgs.append(HumanMessage(content="<observation>ok</observation>"))
    return msgs


@pytest.mark.parametrize("done", [0, 1, 2, 3])
def test_shallow_runs_get_pushed_back(done):
    inner = Recorder()
    llm = FormatReminderLLM(inner, min_steps=4)
    llm.invoke(_with_executes(done))
    text = inner.seen[-1].content
    assert "[depth]" in text
    assert f"only {done} of at least 4" in text, "具体的な数を見せること"
    assert "Do not write <solution> yet" in text


def test_the_push_back_stops_at_the_threshold():
    inner = Recorder()
    llm = FormatReminderLLM(inner, min_steps=4)
    llm.invoke(_with_executes(4))
    assert "[depth]" not in inner.seen[-1].content, "十分掘ったら押し戻さない"


def test_the_format_reminder_is_still_there():
    """深さの押し戻しがあっても、形式の念押しは消えないこと。"""
    inner = Recorder()
    llm = FormatReminderLLM(inner, min_steps=4)
    llm.invoke(_with_executes(0))
    assert TURN_REMINDER in inner.seen[-1].content


def test_zero_disables_the_push_back():
    inner = Recorder()
    llm = FormatReminderLLM(inner, min_steps=0)
    llm.invoke(_with_executes(0))
    assert "[depth]" not in inner.seen[-1].content


def test_only_the_model_s_own_executes_are_counted():
    """observation に <execute> の文字が出ても数えない（水増ししない）。"""
    inner = Recorder()
    llm = FormatReminderLLM(inner, min_steps=4)
    msgs = [
        SystemMessage(content="SYS"),
        HumanMessage(content="<observation>使い方: <execute>...</execute></observation>"),
    ]
    llm.invoke(msgs)
    assert "only 0 of at least 4" in inner.seen[-1].content


def test_the_setting_reaches_the_wrapper():
    from biomni_hypo.config import Settings
    from biomni_hypo.llm import build_agent_llm

    settings = Settings()
    settings.min_exploration_steps = 7
    llm, _handler = build_agent_llm(settings)
    assert llm._min_steps == 7


# ------------------------------------------- 「そんな引数は無い」の言い直し
# 実測: query_reactome(max_result=...) で毎回 1 ステップを捨てていた。
# biomni のツールは名前が似ていて引数が揃っていないので、モデルは
# max_result を持つツールの書き方を、持たないツールにも当てる。


def test_a_bad_keyword_is_turned_into_the_real_signature():
    from biomni_hypo.llm import _signature_hint

    hint = _signature_hint(
        "TypeError: query_reactome() got an unexpected keyword argument 'max_result'"
    )
    assert "`query_reactome` has no parameter `max_result`" in hint
    assert "prompt" in hint and "endpoint" in hint, hint
    assert "Do not guess parameter names" in hint


def test_an_ordinary_observation_gets_no_hint():
    from biomni_hypo.llm import _signature_hint

    assert _signature_hint("BRCA1 の変異が 12 件見つかりました") == ""
    assert _signature_hint("") == ""


def test_an_unknown_function_gets_no_hint():
    """署名を引けないものに、当てずっぽうの助言をしないこと。"""
    from biomni_hypo.llm import _signature_hint

    assert _signature_hint("nosuchtool() got an unexpected keyword argument 'x'") == ""


def test_the_hint_reaches_the_model():
    """観測に付いた助言が、実際に渡す messages に入ること。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from biomni_hypo.llm import FormatReminderLLM

    seen = {}

    class Inner:
        def invoke(self, messages, *a, **k):
            seen["messages"] = messages
            return AIMessage(content="ok")

    llm = FormatReminderLLM(Inner())
    llm.invoke([
        AIMessage(content="<execute>query_reactome(max_result=5)</execute>"),
        HumanMessage(content="TypeError: query_reactome() got an unexpected keyword argument 'max_result'"),
    ])

    text = seen["messages"][-1].content
    assert "has no parameter `max_result`" in text
    assert "prompt" in text


# --------------------------------------- 使えないツールを呼び続けるのを止める
# 実測: query_pubmed を import しようとして失敗し、モジュール名を変えては
# 失敗し、を 28 ステップ繰り返して <solution> に到達しなかった。


@pytest.mark.parametrize(
    "observation",
    [
        "Error: name 'query_pubmed' is not defined",
        "Error: module 'biomni.tool.database' has no attribute 'query_pubmed'",
    ],
)
def test_an_unavailable_tool_is_not_retried(observation):
    from biomni_hypo.llm import _unavailable_hint

    hint = _unavailable_hint(observation)
    assert "`query_pubmed` is not available" in hint
    assert "do not retry" in hint.lower()
    assert "without any import" in hint, "import が要らないことまで言う"


def test_a_normal_observation_gets_no_unavailable_hint():
    from biomni_hypo.llm import _unavailable_hint

    assert _unavailable_hint("BRCA1 に 12 件ヒットしました") == ""


def test_an_import_of_a_loaded_tool_is_told_to_stop_importing():
    """読み込み済みなら「無い」ではない。import をやめさせるのが正解。

    実測: cannot import name 'query_pubmed' from 'biomni.tool.database'。
    事前読み込みしても、モデルが import を書く限り失敗する。
    """
    import biomni_hypo.llm as llm_module

    err = "Error: cannot import name 'query_pubmed' from 'biomni.tool.database'"
    llm_module.PRELOADED_TOOLS.discard("query_pubmed")
    assert "is not available" in llm_module._unavailable_hint(err)

    llm_module.PRELOADED_TOOLS.add("query_pubmed")
    try:
        hint = llm_module._unavailable_hint(err)
        assert "ALREADY loaded" in hint
        assert "Do not import it" in hint
        assert "result = query_pubmed(...)" in hint
    finally:
        llm_module.PRELOADED_TOOLS.discard("query_pubmed")


# ------------------------------------------------- 外部 API のエラーへの助言
# 実測: query_uniprot が fields=function で 400 を返した。UniProt の返却
# フィールド名は UI の見出しと違い、モデルは UI の言葉で書く。


def test_invalid_uniprot_fields_get_a_concrete_fix():
    from biomni_hypo.llm import _api_error_hint

    err = (
        "{'success': False, 'response_url_error': '{\"messages\":["
        "\"Invalid fields parameter value \\'function\\'\","
        "\"Invalid fields parameter value \\'sequences\\'\"]}'}"
    )
    hint = _api_error_hint(err)
    assert "function" in hint and "sequences" in hint
    assert "DROP the `fields=` parameter" in hint, "一番確実な直し方を出す"
    assert "+AND+" in hint, "クエリの連結も間違えている"


def test_a_failed_tool_call_is_not_repeated():
    from biomni_hypo.llm import _api_error_hint

    hint = _api_error_hint("{'success': False, 'error': 'API error: 500'}")
    assert "Do not repeat the same call" in hint


def test_a_successful_observation_gets_no_api_hint():
    from biomni_hypo.llm import _api_error_hint

    assert _api_error_hint("PubMed Results: Title: BushenHuoxue formula ...") == ""
    assert _api_error_hint("{'success': True, 'data': [1, 2]}") == ""


# --------------------------------- 落ちたツールの代わりを名指しする
# 実測: Monarch が落ちたとき、FGFR1 と骨粗鬆症の関連を諦めて
# 「取得できなかった」と書いて終わった。同じ問いは Open Targets でも引ける。


def test_a_failed_source_names_alternatives():
    import biomni_hypo.llm as llm_module

    saved = set(llm_module.PRELOADED_TOOLS)
    llm_module.PRELOADED_TOOLS.clear()
    try:
        hint = llm_module._api_error_hint(
            "{'success': False, 'error': 'query_monarch API error: 500'}"
        )
        assert "query_opentarget" in hint
        assert "Only report a gap if every alternative also failed" in hint
    finally:
        llm_module.PRELOADED_TOOLS.update(saved)


def test_only_loaded_alternatives_are_suggested():
    """無いツールを勧めないこと。勧めれば、それを呼んで 1 ステップ捨てる。"""
    import biomni_hypo.llm as llm_module

    saved = set(llm_module.PRELOADED_TOOLS)
    llm_module.PRELOADED_TOOLS.clear()
    llm_module.PRELOADED_TOOLS.add("query_opentarget")
    try:
        hint = llm_module._api_error_hint(
            "{'success': False, 'error': 'query_monarch API error: 500'}"
        )
        assert "query_opentarget" in hint
        assert "query_gwas_catalog" not in hint, "読み込んでいないものを勧めている"
    finally:
        llm_module.PRELOADED_TOOLS.clear()
        llm_module.PRELOADED_TOOLS.update(saved)


def test_every_alternative_is_a_real_tool():
    """対応表に、存在しないツール名を書いていないこと。"""
    pytest.importorskip("biomni", reason="biomni が無い環境ではスキップ")
    from biomni.utils import read_module2api

    from biomni_hypo.llm import _ALTERNATIVES

    real = {a["name"] for apis in read_module2api().values() for a in apis}
    listed = set(_ALTERNATIVES) | {t for v in _ALTERNATIVES.values() for t in v}
    assert not (listed - real), f"存在しないツール名: {sorted(listed - real)}"


# ------------------------------------------------ 丸ごと print させない
# 実測: UniProt の結果をそのまま print して 10K で切り詰められた。
# 1 回で文脈の数千トークンを失い、得られる情報はほとんど無い。


def test_a_truncated_observation_teaches_what_to_print():
    from biomni_hypo.llm import _api_error_hint

    hint = _api_error_hint(
        "The output is too long to be added to context. "
        "Here are the first 10K characters...\n{...}"
    )
    assert "Never print a whole API result" in hint
    assert "print(list(r.keys())" in hint, "何を print すべきかまで書く"
    assert "Do not re-run the same query" in hint, "見たさに再実行させない"


def test_the_truncation_hint_wins_over_the_generic_one():
    """切り詰めは「API が失敗した」より具体的な助言があるので、そちらを出す。"""
    from biomni_hypo.llm import _api_error_hint

    both = (
        "The output is too long to be added to context. "
        "Here are the first 10K characters...{'success': False}"
    )
    assert "[context]" in _api_error_hint(both)


# ------------------------------------------- 返り値の型を辞書だと思い込む
# 実測: query_pubmed は `-> str` と宣言されているのに、モデルは dict の
# つもりで r['results'] と書いて "string indices must be integers" で落ちた。


def test_a_string_result_indexed_as_a_dict_is_explained():
    import biomni_hypo.llm as llm_module

    llm_module.TOOL_RETURNS["query_pubmed"] = "str"
    try:
        hint = llm_module._return_type_hint(
            "Error: string indices must be integers, not 'str'",
            "<execute>r = query_pubmed(query='FGFR1 osteoporosis')\nprint(r['results'])</execute>",
        )
        assert "`query_pubmed` returns a plain `str`" in hint
        assert "print(r[:800])" in hint
        assert "print(type(r))" in hint
    finally:
        llm_module.TOOL_RETURNS.pop("query_pubmed", None)


def test_an_unknown_tool_gets_the_general_form():
    """型が分からないものに、型を断言しないこと。"""
    from biomni_hypo.llm import _return_type_hint

    hint = _return_type_hint("Error: string indices must be integers", "r = mystery()")
    assert hint.startswith("[type] That tool"), hint


def test_an_unrelated_error_gets_no_type_hint():
    from biomni_hypo.llm import _return_type_hint

    assert _return_type_hint("Error: connection timeout", "r = query_pubmed()") == ""


def test_return_types_are_recorded_from_annotations():
    """注釈がある場合だけ控えること（推測しない）。"""
    pytest.importorskip("biomni", reason="biomni が無い環境ではスキップ")
    import importlib

    from biomni_hypo.agent_factory import _return_type_name

    literature = importlib.import_module("biomni.tool.literature")
    assert _return_type_name(literature.query_pubmed) == "str"
    assert _return_type_name(lambda x: x) == "", "注釈が無ければ空"
