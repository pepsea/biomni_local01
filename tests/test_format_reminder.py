"""毎ターンの出力形式の念押し（docs/design/22）.

タグの規定はシステムプロンプト＝会話の先頭にしか無い。ReAct の手数が増える
ほど生成位置から遠ざかり、num_ctx を超えると古い側から落ちて規定そのものが
消える。結果、biomni に「タグが無い」と差し戻される。

毎ターン、会話の最後尾に短い念押しを置いて、規定を生成の直前に保つ。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from biomni_hypo.llm import TURN_REMINDER, FormatReminderLLM


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
