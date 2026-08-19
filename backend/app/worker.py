"""ラン実行ワーカー.

A1 が生成したコードを API サーバと同じプロセスで実行しないため、
ラン 1 本ごとに子プロセスを起こす（docs/design/02-architecture.md §2.5, §2.6）。
イベントは multiprocessing.Queue で親に返す。

将来: ウォームプール化（A1 の構築コストを償却）。その差し替え点はこのファイルに閉じている。
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import traceback
from typing import Any

log = logging.getLogger(__name__)


def run_in_subprocess(
    run_id: str,
    question: str,
    settings_dict: dict[str, Any],
    queue: mp.Queue[dict[str, Any]],
) -> None:
    """子プロセスのエントリポイント。例外も必ずイベントとして親へ返す。"""
    try:
        from biomni_hypo.config import Settings
        from biomni_hypo.pipeline import run_hypothesis

        settings = Settings(**settings_dict)

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            queue.put({"run_id": run_id, "kind": kind, "payload": payload})

        result = run_hypothesis(question, settings=settings, on_event=on_event, run_id=run_id)
        queue.put({"run_id": run_id, "kind": "result", "payload": result.model_dump(mode="json")})
    except Exception as exc:  # noqa: BLE001 - 子プロセスの例外は必ず親に伝える
        queue.put(
            {
                "run_id": run_id,
                "kind": "error",
                "payload": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-4000:],
                },
            }
        )
    finally:
        queue.put({"run_id": run_id, "kind": "_eof", "payload": {}})


def spawn(run_id: str, question_spec: dict[str, Any], settings_dict: dict[str, Any]) -> tuple[Any, Any]:
    ctx = mp.get_context("spawn")
    queue: Any = ctx.Queue()
    proc = ctx.Process(
        target=run_in_subprocess,
        args=(run_id, question_spec, settings_dict, queue),
        daemon=True,
        name=f"hypo-run-{run_id}",
    )
    proc.start()
    return proc, queue
