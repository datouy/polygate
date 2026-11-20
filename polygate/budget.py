"""跨进程硬预算账本。

预算请求在调用上游前以保守成本上限预占额度，调用结束后按实际 usage 结算。
SQLite 的 ``BEGIN IMMEDIATE`` 保证同一数据库上的检查与插入具备原子性；
多进程共享数据库时不会因为“先查后写”同时穿过预算。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from .db import BatchWriter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_periods (
    client TEXT NOT NULL,
    period_start REAL NOT NULL,
    baseline_cost REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(client, period_start)
);
CREATE TABLE IF NOT EXISTS budget_leases (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    period_start REAL NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    reserved_cost REAL NOT NULL DEFAULT 0,
    actual_cost REAL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_lease_client_period
    ON budget_leases(client, period_start, state, expires_at);
"""


@dataclass(frozen=True)
class BudgetLease:
    id: str
    client: str
    period_start: float
    reserved_cost: float
    expires_at: float


@dataclass(frozen=True)
class BudgetReservation:
    lease: BudgetLease | None
    reason: str | None
    used: float
    limit: float
    reserved: float


def estimate_cost_upper_bound(
    payload: dict,
    pricing: dict,
    model: str,
    *,
    default_max_output_tokens: int = 4096,
    bytes_per_token: float = 2.0,
    completion_is_relevant: bool = True,
) -> float:
    """按请求 JSON 体积与输出上限估算成本，用于预算预占。"""
    price = pricing.get(model) or pricing.get("*") or {}
    input_price = float(price.get("input", 0) or 0) / 1_000_000
    output_price = float(price.get("output", 0) or 0) / 1_000_000
    if input_price <= 0 and output_price <= 0:
        return 0.0
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw = str(payload)
    divisor = max(0.25, float(bytes_per_token or 2.0))
    prompt_tokens = max(1, math.ceil(len(raw.encode("utf-8")) / divisor))
    completion_tokens = 0
    if completion_is_relevant:
        raw_limit = payload.get("max_completion_tokens", payload.get("max_tokens"))
        try:
            completion_tokens = int(raw_limit or default_max_output_tokens)
        except (TypeError, ValueError):
            completion_tokens = int(default_max_output_tokens)
        completion_tokens = max(0, completion_tokens)
    return prompt_tokens * input_price + completion_tokens * output_price


class BudgetLedger:
    """SQLite 预算预占与结算存储。"""

    def __init__(self, db_path: str, writer: BatchWriter | None = None):
        self._writer = writer
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def _flush_pending(self) -> None:
        if self._writer is not None:
            self._writer.flush(timeout=5.0)

    def reserve(
        self,
        *,
        client: str,
        period_start: float,
        limit: float,
        reserved_cost: float,
        baseline_cost: float,
        ttl_seconds: float,
    ) -> BudgetReservation:
        now = time.time()
        expires_at = now + max(1.0, float(ttl_seconds))
        lease_id = uuid.uuid4().hex
        self._flush_pending()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR IGNORE INTO budget_periods(client,period_start,baseline_cost) VALUES(?,?,?)",
                    (client, float(period_start), float(baseline_cost)),
                )
                row = self._conn.execute(
                    "SELECT baseline_cost FROM budget_periods WHERE client=? AND period_start=?",
                    (client, float(period_start)),
                ).fetchone()
                baseline = float(row[0]) if row else 0.0
                settled = self._conn.execute(
                    "SELECT COALESCE(SUM(actual_cost),0) FROM budget_leases "
                    "WHERE client=? AND period_start=? AND state='settled'",
                    (client, float(period_start)),
                ).fetchone()[0]
                active = self._conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost),0) FROM budget_leases "
                    "WHERE client=? AND period_start=? AND state='reserved' AND expires_at>?",
                    (client, float(period_start), now),
                ).fetchone()[0]
                # 成本表可能已经落库，而对应租约结算写入因进程中断/队列异常丢失。
                # 取两者较大值，再做活动预占相加，避免异常恢复时低估预算用量。
                completed = max(baseline + float(settled or 0), float(baseline_cost))
                used = completed + float(active or 0)
                if used + float(reserved_cost) > float(limit) + 1e-9:
                    self._conn.execute("ROLLBACK")
                    return BudgetReservation(
                        lease=None,
                        reason=(
                            f"client monthly budget exceeded "
                            f"({used:.4f} used + {float(reserved_cost):.4f} reserve / {float(limit):g})"
                        ),
                        used=used,
                        limit=float(limit),
                        reserved=float(active or 0) + float(reserved_cost),
                    )
                self._conn.execute(
                    "INSERT INTO budget_leases"
                    "(id,client,period_start,created_at,expires_at,reserved_cost,actual_cost,state) "
                    "VALUES(?,?,?,?,?,?,NULL,'reserved')",
                    (lease_id, client, float(period_start), now, expires_at, float(reserved_cost)),
                )
                self._conn.execute("COMMIT")
                return BudgetReservation(
                    lease=BudgetLease(lease_id, client, float(period_start), float(reserved_cost), expires_at),
                    reason=None,
                    used=used,
                    limit=float(limit),
                    reserved=float(active or 0) + float(reserved_cost),
                )
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def settle(self, lease_id: str, actual_cost: float, *, released: bool = False) -> None:
        state = "released" if released and actual_cost <= 0 else "settled"
        sql = (
            "UPDATE budget_leases SET state=?, actual_cost=?, reserved_cost=0 "
            "WHERE id=? AND state='reserved'"
        )
        params = (state, max(0.0, float(actual_cost)), lease_id)
        if self._writer is not None:
            if self._writer.enqueue(sql, params):
                return
        with self._lock:
            self._conn.execute(sql, params)

    def usage(self, client: str, period_start: float, baseline_cost: float = 0.0) -> dict:
        now = time.time()
        self._flush_pending()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR IGNORE INTO budget_periods(client,period_start,baseline_cost) VALUES(?,?,?)",
                    (client, float(period_start), float(baseline_cost)),
                )
                baseline = self._conn.execute(
                    "SELECT baseline_cost FROM budget_periods WHERE client=? AND period_start=?",
                    (client, float(period_start)),
                ).fetchone()[0]
                settled = self._conn.execute(
                    "SELECT COALESCE(SUM(actual_cost),0) FROM budget_leases "
                    "WHERE client=? AND period_start=? AND state='settled'",
                    (client, float(period_start)),
                ).fetchone()[0]
                active = self._conn.execute(
                    "SELECT COALESCE(SUM(reserved_cost),0) FROM budget_leases "
                    "WHERE client=? AND period_start=? AND state='reserved' AND expires_at>?",
                    (client, float(period_start), now),
                ).fetchone()[0]
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return {
            "baseline": float(baseline or 0),
            "settled": float(settled or 0),
            "reserved": float(active or 0),
            "used": max(float(baseline or 0) + float(settled or 0), float(baseline_cost))
            + float(active or 0),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
