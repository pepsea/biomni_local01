"""Ollama クライアントの構築とヘルスチェック.

biomni.llm.get_llm() の Ollama 分岐は stop_sequences も base_url も渡さない
（docs/design/04-ollama-integration.md §4.1, §4.2）。
stop が効かないと、モデルが </execute> の先に <observation> まで自分で書き、
実行していないコードの「実行結果」を捏造する。根拠提示アプリでは致命的なので、
LLM の構築は必ずこのモジュールを通す。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from biomni_hypo.config import Settings

log = logging.getLogger(__name__)

#: A1 の ReAct ループが依存する stop シーケンス（biomni/agent/a1.py と同じ）
AGENT_STOP_SEQUENCES = ["</execute>", "</solution>"]

#: トークンの送出先。(kind, text) を受け取る。kind は "start" | "token" | "end"
TokenSink = Callable[[str, str], None]


def _callback_base() -> type:
    """LangChain の BaseCallbackHandler。無い環境では素の object にする。

    ChatOllama は pydantic で `callbacks` の型を検証するので、実際に使うときは
    BaseCallbackHandler を継承している必要がある。一方このモジュールは
    langchain 無しの軽量インストールでも import できないと困る（テストと
    ノートブック 03 がそこで動く）。そこで基底クラスだけ動的に決める。
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler

        return BaseCallbackHandler
    except ImportError:  # pragma: no cover - 軽量インストール時のみ
        return object


class TokenStreamHandler(_callback_base()):  # type: ignore[misc]
    """LLM の生成トークンをリアルタイムに横取りするコールバック.

    A1 は `self.llm.invoke(messages)` を同期で呼ぶが、ChatOllama の `_generate` は
    内部でストリーミングしており、チャンクごとに `on_llm_new_token` を発火する。
    つまり **biomni を改変せずにトークン単位の実況が取れる**（実測で確認済み）。

    `sink` はランごとに差し替える。ランの外では None にしておく
    （リソース検索など、ユーザーに見せる必要のない呼び出しも通るため）。
    """

    #: 同期コールバックとして呼んでほしい（別スレッドに逃がさない）
    run_inline = True

    def __init__(self, sink: TokenSink | None = None) -> None:
        super().__init__()
        self.sink = sink
        #: 直近の生成の全文（検証用）
        self.buffer: list[str] = []

    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self.buffer.clear()
        self._emit("start", "")

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self.buffer.clear()
        self._emit("start", "")

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if not token:
            return
        self.buffer.append(token)
        self._emit("token", token)

    def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
        self._emit("end", "")

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._emit("end", "")

    def _emit(self, kind: str, text: str) -> None:
        if self.sink is None:
            return
        try:
            self.sink(kind, text)
        except Exception:  # noqa: BLE001 - 表示のためにランを落とさない
            log.debug("token sink が例外を投げました", exc_info=True)

    @property
    def text(self) -> str:
        return "".join(self.buffer)


@dataclass
class OllamaStatus:
    reachable: bool
    base_url: str
    models: list[str]
    error: str = ""


def ollama_status(base_url: str, timeout: float = 5.0) -> OllamaStatus:
    """Ollama に到達できるか、どのモデルが pull 済みかを返す。"""
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return OllamaStatus(True, base_url, sorted(models))
    except Exception as exc:  # noqa: BLE001 - 接続系はすべて同じ扱いでよい
        return OllamaStatus(False, base_url, [], f"{type(exc).__name__}: {exc}")


#: 実行形態が変わると届かなくなるホスト名の対応表。
#:
#: 同じ 1 台の Ollama でも、呼ぶ側がコンテナの中か外かで名前が変わる。
#: .env は git 管理外なので、Docker 用に設定した host.docker.internal が
#: 残ったままホストで起動する（またはその逆）ことが繰り返し起きた
#: （docs/design/17, 21 §21.3）。届かなければ別名を試す。
ALTERNATE_OLLAMA_HOSTS: dict[str, tuple[str, ...]] = {
    "host.docker.internal": ("localhost", "127.0.0.1"),
    "localhost": ("host.docker.internal",),
    "127.0.0.1": ("host.docker.internal",),
}


@dataclass
class OllamaResolution:
    """実際に使う URL と、設定から変えたなら何から変えたか。"""

    status: OllamaStatus
    changed_from: str = ""

    @property
    def base_url(self) -> str:
        return self.status.base_url


def _swap_host(base_url: str, host: str) -> str:
    parts = urlsplit(base_url)
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_ollama_base_url(base_url: str, timeout: float = 3.0) -> OllamaResolution:
    """設定された URL に届かなければ、実行形態違いの別名を試す。

    見つかった場合は「何から変えたか」を残すこと。黙って別の Ollama を
    掴むと、`ollama list` と画面のモデル一覧が食い違って原因が分からなくなる
    （docs/design/21 §21.15 で実際に起きた）。
    """
    first = ollama_status(base_url, timeout=timeout)
    if first.reachable:
        return OllamaResolution(first)

    host = urlsplit(base_url).hostname or ""
    for alternate in ALTERNATE_OLLAMA_HOSTS.get(host, ()):
        candidate = _swap_host(base_url, alternate)
        status = ollama_status(candidate, timeout=timeout)
        if status.reachable:
            return OllamaResolution(status, changed_from=base_url)
    return OllamaResolution(first)


def build_chat_ollama(
    settings: Settings,
    *,
    model: str | None = None,
    temperature: float | None = None,
    stop: list[str] | None = None,
    fmt: Any = None,
    num_predict: int | None = None,
    callbacks: list[Any] | None = None,
):
    """ChatOllama を組み立てる。差し替え箇所をこの 1 関数に閉じる。

    vLLM / SGLang へ移すときも、クラウド LLM を足すときも、ここだけを触る。

    Args:
        stop: 停止シーケンス。A1 本体に使う場合は AGENT_STOP_SEQUENCES を渡すこと。
        fmt: 構造化出力のスキーマ（Extractor 用）。dict の JSON Schema か "json"。
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise ImportError(
            "langchain-ollama が必要です: pip install langchain-ollama"
        ) from exc

    kwargs: dict[str, Any] = dict(
        model=model or settings.model,
        base_url=settings.ollama_base_url,
        temperature=settings.temperature if temperature is None else temperature,
        num_ctx=settings.num_ctx,
        num_predict=settings.num_predict if num_predict is None else num_predict,
        keep_alive="30m",
    )
    if stop:
        kwargs["stop"] = list(stop)
    if fmt is not None:
        kwargs["format"] = fmt
    if callbacks:
        kwargs["callbacks"] = callbacks
    return ChatOllama(**kwargs)


def build_chat_anthropic(
    settings: Settings,
    *,
    model: str | None = None,
    temperature: float | None = None,
    stop: list[str] | None = None,
    callbacks: list[Any] | None = None,
    streaming: bool = True,
):
    """Claude API のクライアントを組み立てる。

    biomni の Anthropic 分岐は Ollama と違って stop_sequences をきちんと渡すが、
    温度の扱いとストリーミングを揃えたいので、こちらも自前で作る。

    温度について: Claude の 4.6 以降（Opus 5 / Sonnet 5 / Opus 4.8 / 4.7 など）は
    `temperature` を受け付けず 400 を返す。**既定では送らない**。
    """
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise ImportError(
            "langchain-anthropic が必要です: pip install langchain-anthropic"
        ) from exc

    if not settings.anthropic_api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY が設定されていません。Claude API を使うには必要です。"
        )

    kwargs: dict[str, Any] = dict(
        model=model or settings.model,
        anthropic_api_key=settings.anthropic_api_key,
        max_tokens=settings.anthropic_max_tokens,
        streaming=streaming,
    )
    if settings.anthropic_base_url:
        kwargs["anthropic_api_url"] = settings.anthropic_base_url
    if stop:
        kwargs["stop_sequences"] = list(stop)
    if temperature is not None:
        # 呼び出し側が明示したときだけ送る。4.6 以降のモデルでは 400 になる
        kwargs["temperature"] = temperature
    if callbacks:
        kwargs["callbacks"] = callbacks
    return ChatAnthropic(**kwargs)


def build_llm(
    settings: Settings,
    *,
    model: str | None = None,
    temperature: float | None = None,
    stop: list[str] | None = None,
    fmt: Any = None,
    callbacks: list[Any] | None = None,
):
    """プロバイダに応じて LLM を組み立てる。差し替え箇所をここに閉じる。"""
    if settings.provider == "anthropic":
        return build_chat_anthropic(
            settings, model=model, temperature=temperature, stop=stop, callbacks=callbacks
        )
    return build_chat_ollama(
        settings,
        model=model,
        temperature=temperature,
        stop=stop,
        fmt=fmt,
        callbacks=callbacks,
    )


#: 毎ターン、会話の最後尾に足す出力形式の念押し。
#: システムプロンプトにも同じ規定はあるが、それは会話の先頭にあり、
#: ReAct の手数が増えるほど生成位置から遠ざかる。num_ctx を超えると
#: 古い側から落ちるので、規定そのものが消えることすらある。
#: 60 トークン程度を毎ターン払って、規定を生成の直前に置く（docs/design/22）。
TURN_REMINDER = (
    "[format] Reply with your reasoning, then EXACTLY ONE tag: "
    "<execute>...python...</execute> to run code, or "
    "<solution>...</solution> to finish. "
    "A reply with neither tag is discarded. Never write <observation> yourself."
)


def _human_message(text: str) -> Any:
    """HumanMessage を作る（langchain を必須依存にしないため遅延 import）。"""
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=text)


#: 探索が浅いうちに結論へ飛ぼうとするモデルへの押し戻し。
#: 「まだ N 件しか引いていない」と具体的な数を見せるのが要点。
#: 抽象的に「よく調べよ」と言っても効かない。
SHALLOW_NUDGE = (
    "[depth] You have run only {done} of at least {need} data queries. "
    "Do not write <solution> yet. Query another INDEPENDENT source "
    "(a different database or a different aspect) with <execute>."
)


#: `f() got an unexpected keyword argument 'x'` を拾う。
#: biomni のツールは名前が似ていて引数が揃っていない（max_result を持つものと
#: 持たないものがある）ので、モデルは持っていない側にも付けてしまう。
#: 実測: query_reactome(max_result=...) で毎回 1 ステップを捨てていた。
_BAD_KWARG = re.compile(
    r"(\w+)\(\) got an unexpected keyword argument ['\"](\w+)['\"]"
)

#: 実際の引数を並べて言い直させる。抽象的に「引数を確認せよ」では効かない
SIGNATURE_NUDGE = (
    "[tool] `{func}` has no parameter `{bad}`. Its parameters are: {params}. "
    "Call `{func}` again with only those parameters. Do not guess parameter names."
)


def _signature_hint(text: str) -> str:
    """観測に出た「そんな引数は無い」を、正しい引数の並びに変えて返す。"""
    match = _BAD_KWARG.search(text or "")
    if not match:
        return ""
    func, bad = match.group(1), match.group(2)
    params = _tool_parameters(func)
    if not params:
        return ""
    return SIGNATURE_NUDGE.format(func=func, bad=bad, params=", ".join(params))


def _tool_parameters(name: str) -> list[str]:
    """biomni のツールの実際の引数名。見つからなければ空。

    実物の署名を見ること。スキーマ（module2api）は正しいことも多いが、
    最終的に呼ばれるのは関数なので、食い違ったら関数が正しい。
    """
    try:
        import importlib
        import inspect

        from biomni.utils import read_module2api
    except ImportError:  # pragma: no cover - biomni が無い環境
        return []
    try:
        for module_name, apis in (read_module2api() or {}).items():
            if not any(a.get("name") == name for a in apis):
                continue
            func = getattr(importlib.import_module(module_name), name, None)
            if func is None:
                continue
            return [
                p.name
                for p in inspect.signature(func).parameters.values()
                if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            ]
    except Exception:  # noqa: BLE001 - 助言のための処理で落とさない
        return []
    return []


class FormatReminderLLM:
    """invoke のたびに、会話の最後尾へ出力形式の念押しを差し込む薄い包み。

    biomni は `self.llm.invoke(messages)` を呼ぶだけなので、ここで messages を
    加工すれば、biomni を触らずに「毎ターン最後尾」を実現できる。

    state は書き換えない。**この呼び出しに渡す messages のコピーだけ**を変える。
    履歴に念押しが積み上がると、それ自体が context を食うため。

    属性アクセスは元の LLM に委譲する（biomni は `.model_name` や
    `.with_structured_output()` も使う）。
    """

    def __init__(
        self, inner: Any, reminder: str = TURN_REMINDER, min_steps: int = 0
    ) -> None:
        self._inner = inner
        self._reminder = reminder
        #: これだけ <execute> を回すまでは、結論を書かないよう押し戻す
        self._min_steps = max(0, min_steps)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        return self._inner.invoke(self._with_reminder(messages), *args, **kwargs)

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.ainvoke(self._with_reminder(messages), *args, **kwargs)

    def _text_to_append(self, messages: list[Any]) -> str:
        """このターンで足す文。形式の念押し + 必要なら深さの押し戻し。"""
        text = self._reminder
        # 直前の観測が「そんな引数は無い」なら、正しい引数を添える。
        # 放っておくと同じ呼び方を繰り返して、ステップを捨て続ける
        last = messages[-1] if messages else None
        hint = _signature_hint(getattr(last, "content", "") or "")
        if hint:
            text += "\n" + hint
        if self._min_steps:
            done = sum(
                1
                for m in messages
                if getattr(m, "type", "") == "ai"
                and isinstance(getattr(m, "content", None), str)
                and "<execute>" in m.content
            )
            if done < self._min_steps:
                text += "\n" + SHALLOW_NUDGE.format(done=done, need=self._min_steps)
        return text

    def _with_reminder(self, messages: Any) -> Any:
        if not isinstance(messages, list) or not messages:
            return messages
        last = messages[-1]
        content = getattr(last, "content", None)
        if not isinstance(content, str):
            return messages          # マルチモーダル等は触らない
        if self._reminder in content:
            return messages          # 二重に足さない
        addition = self._text_to_append(messages)

        if getattr(last, "type", "") == "human":
            # observation は human として積まれる。そこに足すのが一番自然
            clone = last.model_copy(update={"content": f"{content}\n\n{addition}"})
            return [*messages[:-1], clone]

        # 最後が assistant のことがある（biomni は自分の出力を積んだまま
        # generate に戻ることがある）。そこに足すと、モデルは**自分が書いた
        # 文章の続き**として読む。指示として効かないどころか、指示文ごと
        # 真似される。別の human メッセージとして積む。
        return [*messages, _human_message(addition)]


def build_agent_llm(settings: Settings) -> tuple[Any, TokenStreamHandler]:
    """A1 に差し込む LLM と、そのトークンストリームのハンドラ。

    stop シーケンス付きが必須（§4.1）。ハンドラは戻り値で受け取って、
    ランごとに sink を差し替える。

    さらに、毎ターンの出力形式の念押しで包む（§22）。タグ無し応答で
    biomni に差し戻される事故を減らすため。
    """
    handler = TokenStreamHandler()
    llm = build_llm(settings, stop=AGENT_STOP_SEQUENCES, callbacks=[handler])
    return FormatReminderLLM(llm, min_steps=settings.min_exploration_steps), handler


def hallucinated_observation(raw_text: str) -> bool:
    """LLM の生出力に <observation> が含まれていたら stop が効いていない。

    ノートブック 01 と受け入れテスト AC-1 で使う判定関数。
    """
    return "<observation>" in raw_text
