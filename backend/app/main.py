"""FastAPI アプリ（docs/design/06-api-spec.md）.

ノートブックで検証したコアパッケージ biomni_hypo をそのまま呼ぶ。
ここにドメインロジックを書かないこと。ここは HTTP と SSE の層に徹する。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.store import RunStore
from backend.app.worker import spawn
from biomni_hypo.config import Settings
from biomni_hypo.llm import ollama_status
from biomni_hypo.policy import ResourcePolicy
from biomni_hypo.report import to_markdown
from biomni_hypo.schemas import RunResult
from biomni_hypo.version import __version__

log = logging.getLogger(__name__)

app = FastAPI(title="Biomni Hypothesis Builder", version=__version__)

SETTINGS = Settings()
POLICY = ResourcePolicy.load(SETTINGS.policy_path)
STORE = RunStore(Path(SETTINGS.workspace_path) / "runs.sqlite3")

#: run_id -> 購読者の asyncio.Queue 群
_subscribers: dict[str, set[asyncio.Queue]] = {}
#: run_id -> 次に振る seq
_seq: dict[str, int] = {}
_running: dict[str, Any] = {}


class RunOptions(BaseModel):
    temperature: float | None = None
    num_ctx: int | None = None
    offline_mode: bool | None = None
    use_tool_retriever: bool | None = None
    max_steps: int | None = None
    wallclock_limit_sec: int | None = None
    timeout_seconds: int | None = None
    max_hypotheses: int | None = None


class RunRequest(BaseModel):
    question: str = Field(min_length=1)
    model: str | None = None
    options: RunOptions = Field(default_factory=RunOptions)


# ---------------------------------------------------------------- イベント配信


def _next_seq(run_id: str) -> int:
    _seq[run_id] = _seq.get(run_id, 0) + 1
    return _seq[run_id]


async def _publish(run_id: str, kind: str, payload: dict[str, Any]) -> None:
    """永続化してから配信する（接続していない間の取りこぼしを防ぐ）。"""
    seq = _next_seq(run_id)
    STORE.append_event(run_id, seq, kind, payload)
    event = {"seq": seq, "kind": kind, "payload": payload}
    for q in list(_subscribers.get(run_id, ())):
        q.put_nowait(event)


async def _drain(run_id: str, proc: Any, mp_queue: Any) -> None:
    """子プロセスの multiprocessing.Queue を asyncio 側へ吸い上げる。"""
    loop = asyncio.get_running_loop()
    try:
        while True:
            msg = await loop.run_in_executor(None, mp_queue.get)
            kind = msg["kind"]
            if kind == "_eof":
                break
            if kind == "result":
                run = RunResult.model_validate(msg["payload"])
                STORE.save(run)
                await _publish(run_id, "result", {"run_id": run_id, "status": run.status})
            else:
                await _publish(run_id, kind, msg["payload"])
    finally:
        proc.join(timeout=10)
        if proc.is_alive():  # pragma: no cover - 異常系
            proc.terminate()
        run = STORE.get(run_id)
        if run and run.status == "running":
            run.status = "failed"
            run.error = run.error or "ワーカーが結果を返さずに終了しました"
            run.finished_at = datetime.now(UTC)
            STORE.save(run)
        await _publish(run_id, "done", {"status": run.status if run else "failed"})
        _running.pop(run_id, None)


# -------------------------------------------------------------- エンドポイント


@app.get("/api/health")
async def health() -> dict[str, Any]:
    st = ollama_status(SETTINGS.ollama_base_url, timeout=3)
    return {
        "api": "ok",
        "version": __version__,
        "ollama": {"reachable": st.reachable, "base_url": st.base_url, "models": st.models, "error": st.error},
        "policy_version": POLICY.version,
        "running": list(_running),
        "commercial_mode": SETTINGS.commercial_mode,
    }


@app.get("/api/policy")
async def policy() -> dict[str, Any]:
    return {
        "version": POLICY.version,
        "mode": POLICY.mode,
        "allowed_datasets": POLICY.allowed_dataset_names(),
        "denied_tools": POLICY.denied_tool_names(),
        "allowed_models": POLICY.allowed_model_names(),
    }


@app.get("/api/models")
async def models() -> dict[str, Any]:
    st = ollama_status(SETTINGS.ollama_base_url, timeout=3)
    out = []
    for name in st.models:
        d = POLICY.check_model(name)
        out.append(
            {
                "name": name,
                "license": d.license,
                "allowed": d.allowed,
                "reason": d.reason,
                "loaded": True,
            }
        )
    # pull されていない許可モデルも見せる（何を pull すればよいか分かるように）
    for name in POLICY.allowed_model_names():
        if name not in st.models:
            out.append(
                {
                    "name": name,
                    "license": POLICY.check_model(name).license,
                    "allowed": True,
                    "reason": "",
                    "loaded": False,
                }
            )
    return {"models": out, "ollama": {"reachable": st.reachable, "base_url": st.base_url}}


@app.post("/api/runs", status_code=202)
async def create_run(req: RunRequest) -> dict[str, Any]:
    if _running:
        raise HTTPException(409, {"error": "queue_full", "running": list(_running)})

    settings = SETTINGS.model_copy(deep=True)
    if req.model:
        settings.model = req.model
    for field, value in req.options.model_dump(exclude_none=True).items():
        setattr(settings, field, value)

    decision = POLICY.check_model(settings.model)
    if not decision.allowed:
        raise HTTPException(422, {"error": "policy_violation", "detail": decision.reason})

    run_id = f"r_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    run = RunResult(
        id=run_id,
        question=req.question,
        status="running",
        config=settings.to_run_config(policy_version=POLICY.version),
    )
    STORE.save(run)

    proc, mp_queue = spawn(run_id, req.question, settings.model_dump())
    _running[run_id] = proc
    asyncio.create_task(_drain(run_id, proc, mp_queue))
    await _publish(run_id, "status", {"status": "running"})
    return {"run_id": run_id, "status": "running"}


@app.get("/api/runs")
async def list_runs(limit: int = 50) -> dict[str, Any]:
    return {"runs": STORE.list(limit)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> RunResult:
    run = STORE.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}/report")
async def get_report(run_id: str, format: str = "md") -> Any:
    run = STORE.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if format == "json":
        return run
    return StreamingResponse(iter([to_markdown(run)]), media_type="text/markdown; charset=utf-8")


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    proc = _running.get(run_id)
    if proc is None:
        raise HTTPException(404, "実行中のランではありません")
    proc.terminate()
    run = STORE.get(run_id)
    if run:
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        STORE.save(run)
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    if STORE.get(run_id) is None:
        raise HTTPException(404, "run not found")

    last_id = int(request.headers.get("last-event-id") or 0)
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(run_id, set()).add(queue)

    async def gen():
        try:
            # 再接続時は永続化済みイベントを先に流す
            replayed_done = False
            for ev in STORE.events_since(run_id, last_id):
                yield _sse(ev["seq"], ev["kind"], ev["payload"])
                replayed_done = replayed_done or ev["kind"] == "done"
            # 終了済みのランは、追いつかせたらそこで閉じる（接続を開いたままにしない）
            if replayed_done or run_id not in _running:
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(ev["seq"], ev["kind"], ev["payload"])
                if ev["kind"] == "done":
                    break
        finally:
            _subscribers.get(run_id, set()).discard(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(seq: int, kind: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"id: {seq}\nevent: {kind}\ndata: {body}\n\n"
