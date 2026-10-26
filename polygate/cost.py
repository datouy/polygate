"""按 Key / 模型成本记账。"""

from __future__ import annotations

import sqlite3
import threading
import time

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
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, key_label: str, model: str, prompt: int, completion: int, cost: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO costs(ts,key_label,model,prompt_tokens,completion_tokens,cost) VALUES(?,?,?,?,?,?)",
                (time.time(), key_label, model, int(prompt), int(completion), float(cost)),
            )
            self._conn.commit()

    def summary(self) -> dict:
        with self._lock:
            by_key = self._conn.execute(
                "SELECT key_label, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
                "FROM costs GROUP BY key_label ORDER BY SUM(cost) DESC"
            ).fetchall()
            by_model = self._conn.execute(
                "SELECT model, SUM(cost), SUM(prompt_tokens), SUM(completion_tokens), COUNT(*) "
                "FROM costs GROUP BY model ORDER BY SUM(cost) DESC"
            ).fetchall()
            total = self._conn.execute("SELECT COALESCE(SUM(cost),0) FROM costs").fetchone()[0]
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
        with self._lock:
            self._conn.close()
