"""Ollama クライアントの構築とヘルスチェック.

biomni.llm.get_llm() の Ollama 分岐は stop_sequences も base_url も渡さない
（docs/design/04-ollama-integration.md §4.1, §4.2）。
stop が効かないと、モデルが </execute> の先に <observation> まで自分で書き、
実行していないコードの「実行結果」を捏造する。根拠提示アプリでは致命的なので、
LLM の構築は必ずこのモジュールを通す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from biomni_hypo.config import Settings

#: A1 の ReAct ループが依存する stop シーケンス（biomni/agent/a1.py と同じ）
AGENT_STOP_SEQUENCES = ["</execute>", "</solution>"]


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
    return ChatOllama(**kwargs)


def build_agent_llm(settings: Settings):
    """A1 に差し込む LLM。stop シーケンス付きが必須。"""
    return build_chat_ollama(settings, stop=AGENT_STOP_SEQUENCES)


def hallucinated_observation(raw_text: str) -> bool:
    """LLM の生出力に <observation> が含まれていたら stop が効いていない。

    ノートブック 01 と受け入れテスト AC-1 で使う判定関数。
    """
    return "<observation>" in raw_text
