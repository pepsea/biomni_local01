"""FastAPI アプリ（docs/design/06-api-spec.md）.

ノートブックで検証したコアパッケージ biomni_hypo をそのまま呼ぶ。
ここにドメインロジックを書かないこと。ここは HTTP と SSE の層に徹する。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import queue as queue_mod
import subprocess
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.store import RunStore, StoreUnavailable
from backend.app.worker import spawn, terminate_tree
from biomni_hypo.config import (
    AGENT_DEPENDENCIES,
    ImportReport,
    Settings,
    install_hint,
    missing_dependencies,
    probe_import,
)
from biomni_hypo.llm import ollama_status
from biomni_hypo.models import ModelNotAvailable, apply_model_selection, list_local_models
from biomni_hypo.policy import ResourcePolicy
from biomni_hypo.question import (
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    TEMPLATES,
    QuestionMode,
    ResearchQuestion,
)
from biomni_hypo.report import to_markdown
from biomni_hypo.schemas import RunResult
from biomni_hypo.version import __version__

log = logging.getLogger(__name__)

@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """起動時に biomni を実際に import して確かめる。

    ここで見つけておかないと、ユーザーが質問を投げて子プロセスが落ちるまで
    誰も気付かない。起動ログに出しておけば `make service-logs` で分かる。
    """
    report = await asyncio.get_running_loop().run_in_executor(None, _biomni_probe, True)
    if report.ok:
        log.info("biomni %s を読み込めました（%s）", report.version, report.executable)
    else:
        log.error(
            "biomni を読み込めません。エージェントは起動できません。\n"
            "  使っている Python: %s\n"
            "  詳しくは  /api/health  または  bash scripts/diagnose-app.sh\n%s",
            report.executable or "?",
            report.error,
        )
    yield


app = FastAPI(title="Biomni Hypothesis Builder", version=__version__, lifespan=_lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/history", include_in_schema=False)
async def history_page() -> FileResponse:
    """調査履歴。別ページにしてある。

    入力欄と結果表示で画面が埋まるので、履歴をタブの 5 枚目に置くと
    「そこにある」と気付けない。独立した URL にして、条件も URL に載せる
    （リロードしても共有しても同じ絞り込みが開く）。
    """
    return FileResponse(STATIC_DIR / "history.html")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """入力用の最小 UI（依存なしの 1 ファイル）。

    docs/design/07 の本設計は React を想定しているが、まず「調べたいことを入力して
    走らせる」導線がないと使えないので、ビルド不要の 1 枚を同梱する。
    """
    return FileResponse(STATIC_DIR / "index.html")

SETTINGS = Settings()
POLICY = ResourcePolicy.load(SETTINGS.policy_path)
#: ラン保存。**import 時には開かない**。
#:
#: 以前はここで RunStore(...) を構築していたが、それだと
#: `import backend.app.main` するだけでファイルシステムを触る。
#: 書けない場所（権限、ネットワークマウント）だと、pytest の collection が
#: そこで止まり、**関係の無いテストまで 1 件も走らない**（実測で踏んだ）。
#: 最初に使うときに開く。
_STORE: RunStore | None = None


def store() -> RunStore:
    """ラン保存を返す（初回アクセス時に開く）。"""
    global _STORE
    if _STORE is None:
        _STORE = RunStore(Path(SETTINGS.workspace_path) / "runs.sqlite3")
    return _STORE


# --------------------------------------------------------------- 例外の見せ方
# 素の 500 は本文が空で、画面には `Internal Server Error` の 7 文字しか出ない。
# 手元で 1 人が使う道具なので、理由をそのまま返すほうが役に立つ（実測で踏んだ）。


@app.exception_handler(StoreUnavailable)
async def _store_unavailable(_: Request, exc: StoreUnavailable) -> JSONResponse:
    log.error("ラン保存を開けません: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": {"error": "store_unavailable", "detail": str(exc)}},
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未処理の例外: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "error": "internal",
                "detail": f"{type(exc).__name__}: {exc}",
                "where": f"{request.method} {request.url.path}",
                "traceback": traceback.format_exc()[-2000:],
            }
        },
    )

#: モデル一覧のキャッシュ（/api/tags と /api/show を毎回叩かない）
_model_catalog: Any = None

#: 停止を要求されたラン。_drain が状態を上書きしないようにするため
_cancelled: set[str] = set()

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


class QuestionInput(BaseModel):
    """調べたいことの入力。`text` 以外は任意だが、埋めるほど探索が安定する。"""

    text: str = Field(min_length=1, description="調べたいこと")
    mode: QuestionMode = QuestionMode.HYPOTHESIS
    organism: str = ""
    context: str = ""
    focus: list[str] = Field(default_factory=list)
    background: str = ""
    exclude: list[str] = Field(default_factory=list)
    dataset_ids: list[str] = Field(default_factory=list)
    max_hypotheses: int = Field(default=5, ge=1, le=20)

    def to_question(self) -> ResearchQuestion:
        return ResearchQuestion(**self.model_dump())


class RunRequest(BaseModel):
    #: 構造化入力。`question`（文字列）だけでも受け付ける
    input: QuestionInput | None = None
    question: str | None = Field(default=None, min_length=1)
    #: 省略時は設定のモデル。使えなければローカルから既定を選ぶ
    model: str | None = None
    #: 仮説抽出だけ別モデルにしたい場合
    extractor_model: str | None = None
    options: RunOptions = Field(default_factory=RunOptions)

    def to_question(self) -> ResearchQuestion:
        if self.input is not None:
            return self.input.to_question()
        if self.question:
            return ResearchQuestion.from_text(self.question)
        raise ValueError("input か question のどちらかが必要です")


def _catalog(refresh: bool = False):
    """モデル一覧。pull した直後は `?refresh=true` で取り直す。"""
    global _model_catalog
    if refresh or _model_catalog is None:
        _model_catalog = list_local_models(SETTINGS, POLICY)
    return _model_catalog


# ---------------------------------------------------------------- イベント配信


def _next_seq(run_id: str) -> int:
    _seq[run_id] = _seq.get(run_id, 0) + 1
    return _seq[run_id]


#: 永続化しないイベント。トークンは毎秒数十件流れるうえ、
#: 再接続時に読み直しても意味がない（確定したステップだけ残ればよい）
_EPHEMERAL_EVENTS = {"token"}


async def _publish(run_id: str, kind: str, payload: dict[str, Any]) -> None:
    """永続化してから配信する（接続していない間の取りこぼしを防ぐ）。"""
    seq = _next_seq(run_id)
    if kind not in _EPHEMERAL_EVENTS:
        store().append_event(run_id, seq, kind, payload)
    event = {"seq": seq, "kind": kind, "payload": payload}
    for q in list(_subscribers.get(run_id, ())):
        q.put_nowait(event)


async def _drain(run_id: str, proc: Any, mp_queue: Any) -> None:
    """子プロセスの multiprocessing.Queue を asyncio 側へ吸い上げる。

    **タイムアウト付きで待つこと。** ブロッキングの get だと、停止や異常終了で
    子プロセスが _eof を送らずに死んだときに永久に待ち続け、
    ラン状態が "running" のまま残って次のランが受け付けられなくなる。
    """
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                msg = await loop.run_in_executor(
                    None, functools.partial(mp_queue.get, True, 1.0)
                )
            except queue_mod.Empty:
                if not proc.is_alive():
                    # 子が死んだ。取りこぼしが無いか一度だけ確認して抜ける
                    break
                continue
            kind = msg["kind"]
            if kind == "_eof":
                break
            if kind == "result":
                run = RunResult.model_validate(msg["payload"])
                store().save(run)
                await _publish(run_id, "result", {"run_id": run_id, "status": run.status})
            else:
                await _publish(run_id, kind, msg["payload"])
    finally:
        terminate_tree(proc, grace=5.0)
        cancelled = run_id in _cancelled
        run = store().get(run_id)
        if run and run.status == "running":
            run.status = "cancelled" if cancelled else "failed"
            if not cancelled:
                run.error = run.error or "ワーカーが結果を返さずに終了しました"
            run.finished_at = datetime.now(UTC)
            store().save(run)
        await _publish(run_id, "done", {"status": run.status if run else "failed"})
        _running.pop(run_id, None)
        _cancelled.discard(run_id)


# -------------------------------------------------------------- エンドポイント


#: いま動いているビルドの識別子。プロセスの生存中は変わらない
_BUILD_ID: str | None = None


def _build_id() -> str:
    """git のコミットか、無ければ静的ファイルの更新時刻。

    Docker イメージには .git を入れていないので、コンテナの中では
    ファイルの mtime に落ちる。どちらでも「変わったかどうか」は分かる。
    """
    global _BUILD_ID
    if _BUILD_ID is not None:
        return _BUILD_ID
    root = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            _BUILD_ID = proc.stdout.strip()
            return _BUILD_ID
    except Exception:  # noqa: BLE001 - git が無い環境が普通にある
        pass
    try:
        newest = max(f.stat().st_mtime for f in STATIC_DIR.glob("*.html"))
        _BUILD_ID = datetime.fromtimestamp(newest, UTC).strftime("%m/%d %H:%M")
    except Exception:  # noqa: BLE001
        _BUILD_ID = ""
    return _BUILD_ID


#: biomni の import 判定はキャッシュする。/api/health は頻繁に叩かれるうえ
#: import は重い。壊れている状態は再ビルドするまで変わらないので毎回試さない
_BIOMNI_PROBE: ImportReport | None = None


def _biomni_probe(force: bool = False) -> ImportReport:
    global _BIOMNI_PROBE
    if _BIOMNI_PROBE is None or force:
        _BIOMNI_PROBE = probe_import("biomni")
        if not _BIOMNI_PROBE.ok:
            log.error(
                "biomni を import できません（%s）:\n%s",
                _BIOMNI_PROBE.executable or "?",
                _BIOMNI_PROBE.error,
            )
    return _BIOMNI_PROBE


@app.get("/api/health")
async def health() -> dict[str, Any]:
    st = ollama_status(SETTINGS.ollama_base_url, timeout=3)
    catalog = _catalog()
    default = catalog.default(preferred=SETTINGS.model)
    missing = missing_dependencies(AGENT_DEPENDENCIES)
    biomni_probe = _biomni_probe()
    return {
        "api": "ok",
        "version": __version__,
        # いま動いているのがどのコミットか。「直したはずなのに変わらない」
        # の大半は再ビルドしていないだけなので、画面から一目で分かるようにする
        "build": _build_id(),
        # 依存が欠けていると、ラン開始まで気付かず子プロセスで落ちる。ここで見えるようにする
        "dependencies": {
            "ok": not missing,
            "missing": [{"module": d.module, "package": d.package, "why": d.why} for d in missing],
            "install": install_hint(missing),
        },
        # find_spec は「見つかるか」しか見ない。実際に import して確かめる。
        # 入っているのに読み込めない（壊れた拡張、依存の非互換、途中で失敗した
        # ビルド）を、ここで理由ごと出す
        "biomni": {
            "ok": biomni_probe.ok,
            "version": biomni_probe.version,
            "error": biomni_probe.error,
            "python": biomni_probe.executable,
        },
        "ollama": {"reachable": st.reachable, "base_url": st.base_url, "models": st.models, "error": st.error},
        "models": {
            "configured": SETTINGS.model,
            "default": default.name if default else None,
            "selectable": [m.name for m in catalog.selectable],
            "blocked": [{"name": m.name, "reason": m.reason} for m in catalog.blocked],
        },
        "policy_version": POLICY.version,
        "running": list(_running),
        "commercial_mode": SETTINGS.commercial_mode,
    }


@app.get("/api/providers")
async def providers() -> dict[str, Any]:
    """使える LLM プロバイダ。ローカル完結かどうかを明示する。"""
    catalog = _catalog()
    out = []
    for name, entry in POLICY.providers().items():
        needs = entry.get("requires_env", "")
        if entry.get("local"):
            # ローカルは「Ollama に到達できるか」が準備完了の条件
            ready = catalog.reachable
        else:
            ready = bool(getattr(SETTINGS, "anthropic_api_key", ""))
        out.append(
            {
                "name": name,
                "label": entry.get("label", name),
                "local": bool(entry.get("local")),
                "note": entry.get("note", ""),
                "terms": entry.get("terms", ""),
                "requires_env": needs,
                "ready": ready,
            }
        )
    return {"providers": out, "current": SETTINGS.provider}


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
async def models(refresh: bool = False) -> dict[str, Any]:
    """ローカル（Ollama）にあるモデルを読み込んで返す。

    - 取得済みでポリシー許可 -> `allowed: true`。これが選択肢になる
    - 取得済みだがライセンス不可 -> `allowed: false` + `reason`。**隠さずに理由付きで返す**
    - 未取得の推奨モデル -> `installed: false`。何を pull すればよいか分かるように
    """
    catalog = _catalog(refresh=refresh)
    default = catalog.default(preferred=SETTINGS.model)
    return {
        "models": [m.as_dict() for m in catalog.models],
        "selectable": [m.name for m in catalog.selectable],
        "default": default.name if default else None,
        "configured": SETTINGS.model,
        "ollama": {
            "reachable": catalog.reachable,
            "base_url": catalog.base_url,
            "error": catalog.error,
        },
    }


@app.get("/api/question/templates")
async def question_templates() -> dict[str, Any]:
    """入力欄の初期値。UI の「例から始める」に使う。"""
    return {
        "modes": [
            {"id": m.value, "label": MODE_LABELS[m], "description": MODE_DESCRIPTIONS[m]}
            for m in QuestionMode
        ],
        "templates": [t.as_dict() for t in TEMPLATES],
    }


@app.post("/api/question/preview")
async def question_preview(payload: QuestionInput) -> dict[str, Any]:
    """実行せずに、組み立てたプロンプトと入力の指摘だけを返す。

    何を投げるか分からないまま結果だけ出てくる、という状態を作らないための入口。
    """
    question = payload.to_question()
    hints = question.hints(commercial_mode=SETTINGS.commercial_mode)
    return {
        "summary": question.summary,
        "prompt": question.to_prompt(SETTINGS.prompt_language),
        "prompt_language": SETTINGS.prompt_language,
        "hints": [h.as_dict() for h in hints],
        "can_run": not any(h.severity == "error" for h in hints),
    }


@app.post("/api/runs", status_code=202)
async def create_run(req: RunRequest) -> dict[str, Any]:
    if _running:
        raise HTTPException(409, {"error": "queue_full", "running": list(_running)})

    try:
        question = req.to_question()
    except ValueError as exc:
        raise HTTPException(422, {"error": "invalid_question", "detail": str(exc)}) from exc

    hints = question.hints(commercial_mode=SETTINGS.commercial_mode)
    blocking = [h for h in hints if h.severity == "error"]
    if blocking:
        raise HTTPException(
            422,
            {
                "error": "invalid_question",
                "detail": " / ".join(h.message for h in blocking),
                "hints": [h.as_dict() for h in hints],
            },
        )

    settings = SETTINGS.model_copy(deep=True)
    for field, value in req.options.model_dump(exclude_none=True).items():
        setattr(settings, field, value)
    if req.extractor_model:
        settings.extractor_model = req.extractor_model

    # ローカルのモデルを読んで選択・ライセンス判定・num_ctx の丸めを行う。
    # ノートブックや CLI と同じ関数を通す。
    try:
        _, notes = apply_model_selection(settings, POLICY, model=req.model, catalog=_catalog())
    except ModelNotAvailable as exc:
        raise HTTPException(422, {"error": "model_unavailable", "detail": str(exc)}) from exc

    if settings.extractor_model:
        extractor_decision = POLICY.check_model(settings.extractor_model)
        if not extractor_decision.allowed:
            raise HTTPException(
                422,
                {"error": "model_unavailable", "detail": f"抽出用モデル: {extractor_decision.reason}"},
            )

    run_id = f"r_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    run = RunResult(
        id=run_id,
        question=question.summary,
        question_spec=question.as_spec(),
        prompt=question.to_prompt(settings.prompt_language),
        status="running",
        config=settings.to_run_config(policy_version=POLICY.version),
    )
    run.extra["input_hints"] = [h.as_dict() for h in hints]
    store().save(run)

    proc, mp_queue = spawn(run_id, question.as_spec(), settings.model_dump())
    _running[run_id] = proc
    asyncio.create_task(_drain(run_id, proc, mp_queue))
    await _publish(run_id, "status", {"status": "running", "model": settings.model, "notes": notes})
    return {
        "run_id": run_id,
        "status": "running",
        "model": settings.model,
        "num_ctx": settings.num_ctx,
        "notes": notes,
        "hints": [h.as_dict() for h in hints],
    }


@app.get("/api/runs")
async def list_runs(
    q: str = "",
    provider: str = "",
    model: str = "",
    mode: str = "",
    status: str = "",
    organism: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """過去の調査を条件で絞り込んで探す。

    `q` は質問文・回答・仮説・対象・使用リソース・引用識別子を対象にした部分一致
    （空白区切りで AND）。他は完全一致の絞り込み。
    `facets` に絞り込みに使える値の一覧が入る（UI のプルダウン用）。
    """
    return store().search(
        query=q,
        filters={
            "provider": provider,
            "model": model,
            "mode": mode,
            "status": status,
            "organism": organism,
        },
        since=since,
        until=until,
        limit=min(limit, 200),
        offset=offset,
    )


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> RunResult:
    run = store().get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}/report")
async def get_report(run_id: str, format: str = "md") -> Any:
    run = store().get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if format == "json":
        return run
    return StreamingResponse(iter([to_markdown(run)]), media_type="text/markdown; charset=utf-8")


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    if run_id in _running:
        raise HTTPException(409, {"error": "run_in_progress", "detail": "実行中のランは削除できません"})
    if not store().delete(run_id):
        raise HTTPException(404, "run not found")
    return {"run_id": run_id, "deleted": True}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    """実行中のランを止める。

    子プロセスだけでなく、biomni が起こした孫プロセス（bash / R など）ごと止める。
    状態の確定と done イベントの発行は _drain の finally が行う。
    """
    proc = _running.get(run_id)
    if proc is None:
        raise HTTPException(404, "実行中のランではありません")

    _cancelled.add(run_id)
    run = store().get(run_id)
    if run:
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        store().save(run)
    await _publish(run_id, "status", {"status": "cancelled"})

    # 実際の停止はブロックしうるのでスレッドに逃がす（イベントループを止めない）
    await asyncio.get_running_loop().run_in_executor(
        None, functools.partial(terminate_tree, proc, 5.0)
    )
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    if store().get(run_id) is None:
        raise HTTPException(404, "run not found")

    last_id = int(request.headers.get("last-event-id") or 0)
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(run_id, set()).add(queue)

    async def gen():
        try:
            # 再接続時は永続化済みイベントを先に流す
            replayed_done = False
            for ev in store().events_since(run_id, last_id):
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
