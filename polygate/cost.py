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
    cost REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_costs_key ON costs(key_label);
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

    def add(self, key_label: str, model: str, prompt: int, completion: int, cost: float) -> None:
        sql = (
            "INSERT INTO costs(ts,key_label,model,prompt_tokens,completion_tokens,cost) "
            "VALUES(?,?,?,?,?,?)"
        )
        params = (time.time(), key_label, model, int(prompt), int(completion), float(cost))
        if self._writer is not None:
            self._writer.enqueue(sql, params)
            return
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def summary(self) -> dict:
        by_key_sql = (
            "SELECT key_label, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
            "FROM costs GROUP BY key_label ORDER BY SUM(cost) DESC"
        )
        by_model_sql = (
            "SELECT model, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
            "FROM costs GROUP BY model ORDER BY SUM(cost) DESC"
        )
        total_sql = "SELECT COALESCE(SUM(cost),0) FROM costs"
        if self._writer is not None:
            by_key = self._writer.query(by_key_sql)
            by_model = self._writer.query(by_model_sql)
            total = self._writer.query_one(total_sql)[0]
        else:
            with self._lock:
                by_key = self._conn.execute(by_key_sql).fetchall()
                by_model = self._conn.execute(by_model_sql).fetchall()
                total = self._conn.execute(total_sql).fetchone()[0]
        return {
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
        }

    def close(self) -> None:
        if self._writer is None:
            with self._lock:
                self._conn.close()
