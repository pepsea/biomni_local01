"""Ollama クライアントの構築とヘルスチェック.

biomni.llm.get_llm() の Ollama 分岐は stop_sequences も base_url も渡さない
（docs/design/04-ollama-integration.md §4.1, §4.2）。
stop が効かないと、モデルが </execute> の先に <observation> まで自分で書き、
実行していないコードの「実行結果」を捏造する。根拠提示アプリでは致命的なので、
LLM の構築は必ずこのモジュールを通す。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def build_agent_llm(settings: Settings) -> tuple[Any, TokenStreamHandler]:
    """A1 に差し込む LLM と、そのトークンストリームのハンドラ。

    stop シーケンス付きが必須（§4.1）。ハンドラは戻り値で受け取って、
    ランごとに sink を差し替える。
    """
    handler = TokenStreamHandler()
    llm = build_llm(settings, stop=AGENT_STOP_SEQUENCES, callbacks=[handler])
    return llm, handler


def hallucinated_observation(raw_text: str) -> bool:
    """LLM の生出力に <observation> が含まれていたら stop が効いていない。

    ノートブック 01 と受け入れテスト AC-1 で使う判定関数。
    """
    return "<observation>" in raw_text
