"""Provider 兼容性探测结果存储。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from .db import BatchWriter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compatibility_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    public_model TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    probe TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    latency_ms REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_compat_run ON compatibility_results(run_id, id);
CREATE INDEX IF NOT EXISTS idx_compat_route ON compatibility_results(provider, public_model, upstream_model, id);
"""


class CompatibilityStore:
    """保存管理员触发的兼容性探测结果；SQLite 不可用时退化为进程内存。"""

    def __init__(self, db_path: str | None = None, writer: BatchWriter | None = None):
        self._writer = writer
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._memory: list[dict] = []
        if db_path:
            if writer is not None:
                self._conn = writer.conn
                self._lock = writer.lock
                writer.executescript(_SCHEMA)
            else:
                self._conn = sqlite3.connect(db_path, check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.executescript(_SCHEMA)
                self._conn.commit()

    def record(
        self,
        *,
        run_id: str,
        provider: str,
        public_model: str,
        upstream_model: str,
        probe: str,
        ok: bool,
        status_code: int | None,
        latency_ms: float,
        detail: dict | None = None,
    ) -> None:
        row = {
            "run_id": run_id,
            "ts": time.time(),
            "provider": provider,
            "public_model": public_model,
            "upstream_model": upstream_model,
            "probe": probe,
            "ok": bool(ok),
            "status_code": status_code,
            "latency_ms": float(latency_ms),
            "detail": detail or {},
        }
        if self._conn is None:
            with self._lock:
                self._memory.append(row)
                del self._memory[:-2000]
            return
        sql = (
            "INSERT INTO compatibility_results"
            "(run_id,ts,provider,public_model,upstream_model,probe,ok,status_code,latency_ms,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            row["run_id"], row["ts"], row["provider"], row["public_model"],
            row["upstream_model"], row["probe"], int(row["ok"]), row["status_code"],
            row["latency_ms"], json.dumps(row["detail"], ensure_ascii=False),
        )
        if self._writer is not None:
            self._writer.enqueue(sql, params)
            return
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def latest(self, run_id: str | None = None, limit: int = 1000) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        if self._conn is None:
            with self._lock:
                rows = list(self._memory)
            if run_id is None and rows:
                run_id = rows[-1]["run_id"]
            rows = [r for r in rows if run_id is None or r["run_id"] == run_id]
            return rows[-limit:]

        if self._writer is not None:
            self._writer.flush()
        if run_id is None:
            sql = "SELECT run_id FROM compatibility_results ORDER BY id DESC LIMIT 1"
            with self._lock:
                row = self._conn.execute(sql).fetchone()
            if not row:
                return []
            run_id = row[0]
        sql = (
            "SELECT run_id,ts,provider,public_model,upstream_model,probe,ok,status_code,latency_ms,detail "
            "FROM compatibility_results WHERE run_id=? ORDER BY id ASC LIMIT ?"
        )
        with self._lock:
            values = self._conn.execute(sql, (run_id, limit)).fetchall()
        out: list[dict] = []
        for row in values:
            try:
                detail = json.loads(row[9]) if row[9] else {}
            except Exception:
                detail = {}
            out.append({
                "run_id": row[0],
                "ts": row[1],
                "provider": row[2],
                "model": row[3],
                "upstream_model": row[4],
                "probe": row[5],
                "ok": bool(row[6]),
                "status_code": row[7],
                "latency_ms": row[8],
                "detail": detail,
            })
        return out

    def close(self) -> None:
        if self._conn is not None and self._writer is None:
            with self._lock:
                self._conn.close()
                self._conn = None
