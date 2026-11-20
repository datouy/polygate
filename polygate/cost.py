"""按 Key / 模型成本记账。"""

from __future__ import annotations

import sqlite3
import threading
import time

from .db import BatchWriter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_label TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    client TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_costs_key ON costs(key_label);
CREATE INDEX IF NOT EXISTS idx_costs_ts ON costs(ts);
CREATE INDEX IF NOT EXISTS idx_costs_client_ts ON costs(client, ts);
"""


def compute_cost(prompt_tokens: int, completion_tokens: int, pricing: dict, model: str) -> float:
    """根据 token 数与定价（每百万 token 价格）计算成本。"""
    p = pricing.get(model) or pricing.get("*")
    if not p:
        return 0.0
    in_price = float(p.get("input", 0) or 0) / 1_000_000
    out_price = float(p.get("output", 0) or 0) / 1_000_000
    return prompt_tokens * in_price + completion_tokens * out_price


class CostTracker:
    def __init__(self, db_path: str, writer: BatchWriter | None = None):
        self._writer = writer
        if writer is not None:
            self._conn = writer.conn
            self._lock = writer.lock
            writer.executescript(_SCHEMA)
        else:
            self._lock = threading.Lock()
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """为旧库补齐 client 列。"""
        with self._lock:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(costs)")}
            if "client" not in cols:
                self._conn.execute("ALTER TABLE costs ADD COLUMN client TEXT NOT NULL DEFAULT ''")
            try:
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_costs_client_ts ON costs(client, ts)")
            except Exception:
                pass
            self._conn.commit()

    def add(self, key_label: str, model: str, prompt: int, completion: int, cost: float, client: str = "") -> None:
        sql = "INSERT INTO costs(ts,key_label,model,prompt_tokens,completion_tokens,cost,client) VALUES(?,?,?,?,?,?,?)"
        params = (time.time(), key_label, model, int(prompt), int(completion), float(cost), client or "")
        if self._writer is not None:
            self._writer.enqueue(sql, params)
            return
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def spend_since(self, client: str | None = None, since: float | None = None) -> float:
        """统计某客户端自 since 以来的累计成本（预算检查用）。"""
        conds: list[str] = []
        params: list = []
        if client:
            conds.append("client = ?")
            params.append(client)
        if since is not None:
            conds.append("ts >= ?")
            params.append(float(since))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        sql = f"SELECT COALESCE(SUM(cost),0) FROM costs{where}"
        if self._writer is not None:
            row = self._writer.query_one(sql, tuple(params))
        else:
            with self._lock:
                row = self._conn.execute(sql, tuple(params)).fetchone()
        return float(row[0]) if row else 0.0

    def summary(self) -> dict:
        # The operational panel reports the recent billing window. Quota checks
        # use spend_since(), so this no longer scans the full lifetime table.
        since = time.time() - 30 * 86400
        by_key_sql = (
            "SELECT key_label, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
            "FROM costs WHERE ts >= ? GROUP BY key_label ORDER BY SUM(cost) DESC"
        )
        by_model_sql = (
            "SELECT model, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
            "FROM costs WHERE ts >= ? GROUP BY model ORDER BY SUM(cost) DESC"
        )
        total_sql = "SELECT COALESCE(SUM(cost),0) FROM costs WHERE ts >= ?"
        by_client_sql = (
            "SELECT client, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
            "FROM costs WHERE ts >= ? GROUP BY client ORDER BY SUM(cost) DESC"
        )
        params = (since,)
        if self._writer is not None:
            by_key = self._writer.query(by_key_sql, params)
            by_model = self._writer.query(by_model_sql, params)
            total = self._writer.query_one(total_sql, params)[0]
            by_client = self._writer.query(by_client_sql, params)
        else:
            with self._lock:
                by_key = self._conn.execute(by_key_sql, params).fetchall()
                by_model = self._conn.execute(by_model_sql, params).fetchall()
                total = self._conn.execute(total_sql, params).fetchone()[0]
                by_client = self._conn.execute(by_client_sql, params).fetchall()
        return {
            "scope": "rolling",
            "window_days": 30,
            "total": round(total, 6),
            "by_key": [
                {
                    "key": r[0],
                    "cost": round(r[1], 6),
                    "prompt_tokens": r[2],
                    "completion_tokens": r[3],
                    "calls": r[4],
                }
                for r in by_key
            ],
            "by_model": [
                {
                    "model": r[0],
                    "cost": round(r[1], 6),
                    "prompt_tokens": r[2],
                    "completion_tokens": r[3],
                    "calls": r[4],
                }
                for r in by_model
            ],
            "by_client": [
                {
                    "client": r[0] or "",
                    "cost": round(r[1], 6),
                    "prompt_tokens": r[2],
                    "completion_tokens": r[3],
                    "calls": r[4],
                }
                for r in by_client
            ],
        }

    def close(self) -> None:
        if self._writer is None:
            with self._lock:
                self._conn.close()
