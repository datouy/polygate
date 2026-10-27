"""请求日志与重放（SQLite 持久化）。"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time

from .db import BatchWriter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT,
    key_label TEXT,
    status TEXT NOT NULL,
    status_code INTEGER,
    latency_ms REAL DEFAULT 0,
    request_json TEXT,
    response_json TEXT,
    error TEXT,
    upstream_model TEXT,
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""

_MAX_RESPONSE = 20_000


def _trim_response_structurally(response):
    """把超大响应压缩成仍可解析的结构：只保留 choices[0].message.content 前 N 字符。

    返回 None 表示无法安全压缩（交由调用方整体丢弃 + 标记 truncated）。
    """
    try:
        data = copy.deepcopy(response)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            budget = max(0, _MAX_RESPONSE // 2)
            if len(msg["content"]) > budget:
                msg["content"] = msg["content"][:budget] + "…[truncated]"
            return data
    return None


def _encode_response(response) -> tuple[str | None, bool]:
    """把 response 编码成合法 JSON；超长时结构裁剪或整体丢弃，绝不切字符破坏 JSON。"""
    if response is None:
        return None, False
    try:
        text = json.dumps(response, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, False
    if len(text) <= _MAX_RESPONSE:
        return text, False

    trimmed = _trim_response_structurally(response)
    if trimmed is None:
        return None, True
    try:
        text2 = json.dumps(trimmed, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, True
    if len(text2) <= _MAX_RESPONSE:
        return text2, True
    return None, True


class RequestLog:
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
        """为旧库补齐 upstream_model / truncated 两列。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
        if "upstream_model" not in cols:
            self._conn.execute("ALTER TABLE requests ADD COLUMN upstream_model TEXT")
        if "truncated" not in cols:
            self._conn.execute("ALTER TABLE requests ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()

    def log(
        self,
        *,
        req_id: str,
        endpoint: str,
        model: str,
        provider: str,
        key_label: str,
        status: str,
        status_code: int | None = 200,
        latency_ms: float = 0.0,
        request=None,
        response=None,
        error: str | None = None,
        upstream_model: str | None = None,
    ) -> None:
        req_json = json.dumps(request, ensure_ascii=False) if request is not None else None
        resp_json, truncated = _encode_response(response)
        sql = (
            "INSERT INTO requests(req_id,ts,endpoint,model,provider,key_label,status,"
            "status_code,latency_ms,request_json,response_json,error,upstream_model,truncated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            req_id, time.time(), endpoint, model, provider, key_label, status,
            status_code, latency_ms, req_json, resp_json, error, upstream_model,
            int(truncated),
        )
        if self._writer is not None:
            self._writer.enqueue(sql, params)
            return
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        sql = (
            "SELECT req_id,ts,endpoint,model,provider,key_label,status,status_code,latency_ms "
            "FROM requests ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        if self._writer is not None:
            rows = self._writer.query(sql, (limit, offset))
        else:
            with self._lock:
                rows = self._conn.execute(sql, (limit, offset)).fetchall()
        return [
            {
                "req_id": r[0],
                "ts": r[1],
                "endpoint": r[2],
                "model": r[3],
                "provider": r[4],
                "key": r[5],
                "status": r[6],
                "status_code": r[7],
                "latency_ms": r[8],
            }
            for r in rows
        ]

    def get(self, req_id: str) -> dict | None:
        sql = (
            "SELECT req_id,ts,endpoint,model,provider,key_label,status,status_code,latency_ms,"
            "request_json,response_json,error,upstream_model,truncated "
            "FROM requests WHERE req_id=?"
        )
        if self._writer is not None:
            row = self._writer.query_one(sql, (req_id,))
        else:
            with self._lock:
                row = self._conn.execute(sql, (req_id,)).fetchone()
        if not row:
            return None
        return {
            "req_id": row[0],
            "ts": row[1],
            "endpoint": row[2],
            "model": row[3],
            "provider": row[4],
            "key": row[5],
            "status": row[6],
            "status_code": row[7],
            "latency_ms": row[8],
            "request": json.loads(row[9]) if row[9] else None,
            "response": json.loads(row[10]) if row[10] else None,
            "error": row[11],
            "upstream_model": row[12],
            "truncated": bool(row[13]),
        }

    def close(self) -> None:
        if self._writer is None:
            with self._lock:
                self._conn.close()
