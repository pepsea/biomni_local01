"""ランの永続化と検索.

RunResult の JSON をそのまま持ちつつ、検索に使う条件と結果を列に展開する。
「どの条件で調べた結果か」を後から辿れることが目的（docs/design/14）。

Postgres へ移す場合もこのモジュールの差し替えだけで済むようにインタフェースを閉じる。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from biomni_hypo.schemas import RunResult

#: runs テーブルの列（id / payload 以外）。移行時にこの定義を見て ALTER する
RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("question", "TEXT"),
    ("status", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    # --- 条件 ---
    ("provider", "TEXT"),
    ("model", "TEXT"),
    ("mode", "TEXT"),
    ("organism", "TEXT"),
    ("context", "TEXT"),
    ("focus", "TEXT"),
    ("offline_mode", "INTEGER"),
    ("policy_version", "INTEGER"),
    # --- 結果 ---
    ("answer", "TEXT"),
    ("hypothesis_count", "INTEGER"),
    ("unsupported_count", "INTEGER"),
    ("evidence_verified", "INTEGER"),
    ("evidence_failed", "INTEGER"),
    ("step_count", "INTEGER"),
    ("duration_sec", "REAL"),
    # --- 検索対象をまとめたもの ---
    ("search_text", "TEXT"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL
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

#: 検索でそのまま渡してよい絞り込み列
FILTER_COLUMNS = ("provider", "model", "mode", "status", "organism")


class StoreUnavailable(RuntimeError):
    """DB を開けない。原因が分かる形にして投げ直す。

    sqlite の "unable to open database file" は、実際には
    「権限が無い」「親ディレクトリが作れない」「ネットワークマウントで
    ロックが効かない」のどれでも同じ文言になる。そのままでは打つ手が無い。
    """


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreUnavailable(
                f"保存先のディレクトリを作れません: {self.path.parent.resolve()}\n"
                f"  {type(exc).__name__}: {exc}\n"
                f"  .env の HYPO_WORKSPACE を書ける場所に変えてください。"
            ) from exc
        try:
            with self._connect() as conn:
                conn.executescript(SCHEMA)
                self._migrate(conn)
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(self._why_unavailable(exc)) from exc

    def _why_unavailable(self, exc: Exception) -> str:
        """開けない理由を、その場で調べて添える。"""
        parent = self.path.parent
        facts = [
            f"データベースを開けません: {self.path.resolve()}",
            f"  sqlite: {exc}",
            f"  ディレクトリが在る : {parent.is_dir()}",
            f"  ディレクトリに書ける: {os.access(parent, os.W_OK)}",
        ]
        if self.path.exists():
            facts.append(f"  ファイルに書ける    : {os.access(self.path, os.W_OK)}")
        # ネットワークマウントは sqlite のロックと相性が悪い。よくある原因なので
        # 当てはまりそうなら名指しする
        resolved = str(parent.resolve())
        if resolved.startswith(("/mnt/", "/media/", "/net/")) or "nfs" in resolved:
            facts.append(
                "  → マウントされた場所に見えます。NFS などロックの効かない"
                "ファイルシステムでは sqlite を開けないことがあります。"
            )
        facts.append(
            "  .env の HYPO_WORKSPACE をローカルディスクの書ける場所に変えてください"
            "（例: HYPO_WORKSPACE=$HOME/.biomni-hypo/workspace）。"
        )
        return "\n".join(facts)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """不足している列を足す。既存の DB を作り直さずに済ませる。"""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        added = False
        for name, kind in RUN_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
                added = True
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_provider ON runs(provider)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model)")
        if added:
            # 既存行を payload から埋め直す
            for row in conn.execute("SELECT id, payload FROM runs").fetchall():
                try:
                    run = RunResult.model_validate_json(row["payload"])
                except Exception:  # noqa: BLE001 - 壊れた行はそのままにする
                    continue
                fields = _extract(run)
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(
                    f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), row["id"])
                )

    # ------------------------------------------------------------------ runs

    def save(self, run: RunResult) -> None:
        fields = _extract(run)
        fields["payload"] = run.model_dump_json()
        columns = ["id", *fields]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if existing and existing["created_at"]:
                fields["created_at"] = existing["created_at"]
            conn.execute(
                f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                (run.id, *fields.values()),
            )

    def get(self, run_id: str) -> RunResult | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunResult.model_validate_json(row["payload"]) if row else None

    def delete(self, run_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        return cur.rowcount > 0

    def search(
        self,
        *,
        query: str = "",
        filters: dict[str, str] | None = None,
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """条件で絞り込んでランを探す。

        Args:
            query: 質問文・回答・仮説・対象などを対象にした部分一致（空白区切りの AND）。
            filters: provider / model / mode / status / organism の完全一致。
            since / until: created_at の範囲（ISO 文字列の前方比較）。

        Returns:
            {"runs": [...], "total": n, "facets": {...}}
        """
        where: list[str] = []
        params: list[Any] = []

        for term in (query or "").split():
            where.append("search_text LIKE ?")
            params.append(f"%{term.lower()}%")

        for key, value in (filters or {}).items():
            if key in FILTER_COLUMNS and value:
                where.append(f"{key} = ?")
                params.append(value)

        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at <= ?")
            params.append(until)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        listed = ", ".join(name for name, _ in RUN_COLUMNS if name != "search_text")

        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM runs {clause}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT id, {listed} FROM runs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            facets = {
                column: [
                    r[0]
                    for r in conn.execute(
                        f"SELECT {column} FROM runs WHERE {column} IS NOT NULL AND {column} != '' "
                        f"GROUP BY {column} ORDER BY COUNT(*) DESC LIMIT 30"
                    ).fetchall()
                ]
                for column in FILTER_COLUMNS
            }

        return {"runs": [dict(r) for r in rows], "total": total, "facets": facets}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """後方互換。search() の薄いラッパ。"""
        return self.search(limit=limit)["runs"]

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


def _extract(run: RunResult) -> dict[str, Any]:
    """RunResult から、検索に使う条件と結果を取り出す。"""
    spec = run.question_spec or {}
    focus = spec.get("focus") or []
    now = datetime.now(UTC).isoformat()
    verification = run.verification

    searchable = [
        run.question,
        run.answer,
        spec.get("organism", ""),
        spec.get("context", ""),
        spec.get("background", ""),
        " ".join(str(f) for f in focus),
        run.config.model,
        run.config.provider,
        spec.get("mode", ""),
        *[h.statement for h in run.hypotheses],
        *[h.statement for h in run.unsupported_ideas],
        *[r.name for r in run.resources_used],
        *[e.identifier for h in run.hypotheses for e in h.evidence],
        *[e.identifier for e in run.answer_evidence],
        # 論点も検索対象にする。「なぜその結論になったか」で過去のランを引けるように
        *[p.point for p in run.answer_reasoning],
        *[p.finding for p in run.answer_reasoning],
        *[e.identifier for p in run.answer_reasoning for e in p.evidence],
        *run.answer_uncertainties,
        # 計画も検索対象に。「何をやろうとしたか」で過去のランを引ける
        *[i.text for i in run.plan],
    ]

    return {
        "question": run.question,
        "status": run.status,
        "created_at": run.started_at.isoformat(),
        "updated_at": now,
        "provider": run.config.provider,
        "model": run.config.model,
        "mode": spec.get("mode", ""),
        "organism": spec.get("organism", ""),
        "context": spec.get("context", ""),
        "focus": ", ".join(str(f) for f in focus),
        "offline_mode": int(bool(run.config.offline_mode)),
        "policy_version": run.config.policy_version,
        "answer": run.answer,
        "hypothesis_count": len(run.hypotheses),
        "unsupported_count": len(run.unsupported_ideas),
        "evidence_verified": verification.verified,
        "evidence_failed": verification.failed,
        "step_count": len(run.steps),
        "duration_sec": float(run.extra.get("duration_sec") or 0.0),
        "search_text": " ".join(x for x in searchable if x).lower(),
    }
