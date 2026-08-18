"""ランの永続化.

v1 は sqlite3 に RunResult の JSON をそのまま入れる（依存を増やさないため）。
Postgres へ移す場合もこのモジュールの差し替えだけで済むようにインタフェースを閉じる。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from biomni_hypo.schemas import RunResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ runs

    def save(self, run: RunResult) -> None:
        now = datetime.now(UTC).isoformat()
        payload = run.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO runs (id, question, status, created_at, updated_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload""",
                (run.id, run.question, run.status, now, now, payload),
            )

    def get(self, run_id: str) -> RunResult | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunResult.model_validate_json(row["payload"]) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, question, status, created_at, updated_at FROM runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- events

    def append_event(self, run_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
        """イベントは配信前に永続化する。SSE に繋いでいなくても取りこぼさない。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO events (run_id, seq, kind, payload) VALUES (?, ?, ?, ?)",
                (run_id, seq, kind, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def events_since(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, kind, payload FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload"])} for r in rows]
