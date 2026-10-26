"""SQLite 用量持久化。"""

from __future__ import annotations

import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
"""


class UsageStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(
        self,
        model: str,
        provider: str,
        status: str,
        prompt: int = 0,
        completion: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls(ts,model,provider,status,prompt_tokens,completion_tokens,latency_ms) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), model, provider, status, int(prompt), int(completion), float(latency_ms)),
            )
            self._conn.commit()

    def total_calls(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    def daily_summary(self, days: int = 7) -> list[dict]:
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(ts,'unixepoch','localtime') AS day,"
                " COUNT(*) AS calls,"
                " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
                " SUM(prompt_tokens) AS prompt_tokens,"
                " SUM(completion_tokens) AS completion_tokens,"
                " ROUND(AVG(latency_ms),1) AS avg_latency_ms"
                " FROM calls WHERE ts >= ? GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        return [
            {
                "day": r[0],
                "calls": r[1],
                "errors": r[2] or 0,
                "prompt_tokens": r[3] or 0,
                "completion_tokens": r[4] or 0,
                "avg_latency_ms": r[5] or 0.0,
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
