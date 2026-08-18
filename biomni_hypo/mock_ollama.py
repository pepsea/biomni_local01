"""検証用のモック Ollama サーバ.

本物の Ollama が無い環境（CI、ネットワーク制限下）でも、
**LLM に渡した設定が実際に HTTP リクエストへ乗るか**を確かめられるようにする。

これで検証できること:
  - `build_chat_ollama()` が stop / num_ctx / format を options に載せているか（§4.1）
  - `biomni.llm.get_llm(source="Ollama")` が stop を落としていること（§4.1 の不具合）
  - 応答を台本で与えて、A1 の ReAct ループと TracingRunner を通せること

検証できないこと: モデルが実際に指示に従うか。それは実機の notebooks/01 で見る。
本番コードからは import しない。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class MockOllama:
    """`with MockOllama(replies=[...]) as mock:` で使う。

    Attributes:
        replies: /api/chat が順に返す assistant のテキスト。尽きたら最後を繰り返す。
        models: /api/tags が返すモデル名。
        requests: 受け取ったリクエストの生ボディ（検証用）。
    """

    replies: list[str] = field(default_factory=lambda: ["ok"])
    #: /api/tags が返すモデル。名前だけ、または (名前, サイズ, context長) のタプル
    models: list[Any] = field(default_factory=lambda: ["qwen3:14b"])
    requests: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _cursor: int = 0

    @property
    def base_url(self) -> str:
        assert self._server is not None, "サーバが起動していません"
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/api/chat"]

    def last_options(self) -> dict[str, Any]:
        """直近の /api/chat で送られた options（stop / num_ctx などが入る）。"""
        return self.chat_requests[-1]["body"].get("options", {}) if self.chat_requests else {}

    def _next_reply(self) -> str:
        if not self.replies:
            return ""
        reply = self.replies[min(self._cursor, len(self.replies) - 1)]
        self._cursor += 1
        return reply

    def __enter__(self) -> MockOllama:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # ログを黙らせる
                pass

            def _json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body: dict[str, Any]) -> None:
                mock.requests.append({"path": self.path.split("?")[0], "body": body})

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                self._record({})
                if path == "/api/tags":
                    self._json({"models": [_tag_entry(m) for m in mock.models]})
                elif path in ("/", "/api/version"):
                    self._json({"version": "mock"})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                self._record(body)

                path = self.path.split("?")[0]
                if path == "/api/show":
                    name = body.get("model") or body.get("name") or ""
                    self._json(_show_entry(name, mock.models))
                    return
                if path != "/api/chat":
                    self._json({"error": "not found"}, 404)
                    return

                content = mock._next_reply()
                if body.get("stream"):
                    self._stream_chat(body.get("model", "mock"), content)
                else:
                    self._json(_chat_final(body.get("model", "mock"), content))

            def _stream_chat(self, model: str, content: str) -> None:
                chunks = [
                    json.dumps(
                        {
                            "model": model,
                            "created_at": "2024-01-01T00:00:00Z",
                            "message": {"role": "assistant", "content": content},
                            "done": False,
                        }
                    ),
                    json.dumps(_chat_final(model, "")),
                ]
                payload = ("\n".join(chunks) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        # keep-alive があるので必ずスレッド版を使う（単一スレッドだと 2 本目で詰まる）
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _spec(entry: Any) -> tuple[str, int, int]:
    """"qwen3:14b" または ("qwen3:14b", size_bytes, context_length) を正規化する。"""
    if isinstance(entry, (tuple, list)):
        name = str(entry[0])
        size = int(entry[1]) if len(entry) > 1 else 1
        ctx = int(entry[2]) if len(entry) > 2 else 40960
        return name, size, ctx
    return str(entry), 1, 40960


def _tag_entry(entry: Any) -> dict[str, Any]:
    name, size, _ctx = _spec(entry)
    family = name.split(":", 1)[0].rsplit("/", 1)[-1]
    return {
        "name": name,
        "model": name,
        "modified_at": "2024-01-01T00:00:00Z",
        "size": size,
        "digest": "0" * 64,
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": family,
            "families": [family],
            "parameter_size": "14.8B",
            "quantization_level": "Q4_K_M",
        },
    }


def _show_entry(name: str, entries: list[Any]) -> dict[str, Any]:
    ctx = 40960
    for entry in entries:
        entry_name, _size, entry_ctx = _spec(entry)
        if entry_name == name:
            ctx = entry_ctx
            break
    family = name.split(":", 1)[0].rsplit("/", 1)[-1] or "mock"
    return {
        "license": "mock",
        "details": {"family": family, "format": "gguf"},
        "model_info": {
            "general.architecture": family,
            f"{family}.context_length": ctx,
            f"{family}.embedding_length": 5120,
        },
        "capabilities": ["completion", "tools"],
    }


def _chat_final(model: str, content: str) -> dict[str, Any]:
    return {
        "model": model,
        "created_at": "2024-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1,
        "load_duration": 1,
        "prompt_eval_count": 1,
        "prompt_eval_duration": 1,
        "eval_count": 1,
        "eval_duration": 1,
    }
