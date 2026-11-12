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
    logical_id TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT,
    key_label TEXT,
    client TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_requests_client_ts ON requests(client, ts);
"""

_MAX_RESPONSE = 20_000
# 请求体落库上限：响应侧截断了，请求侧也必须有上限，
# 否则一个 500KB 的 prompt 会把日志库撑爆（实测可落盘 500061 字节）。
_MAX_REQUEST = 20_000


def _trim_request_structurally(request):
    """把超大请求体压缩成仍可解析的结构：只保留每个 message 的 content 前 N 字符。

    返回 None 表示无法安全压缩（交由调用方整体丢弃 + 标记 truncated）。
    """
    try:
        data = copy.deepcopy(request)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    budget = max(0, _MAX_REQUEST // 2)
    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and len(content) > budget:
                msg["content"] = content[:budget] + "…[truncated]"
        return data
    # 非 messages 结构（如 embeddings 的 input 数组）：按整体长度裁剪
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw) <= _MAX_REQUEST:
        return data
    return None


def _encode_request(request) -> tuple[str | None, bool]:
    """把 request 编码成合法 JSON；超长时结构裁剪或整体丢弃，绝不切字符破坏 JSON。"""
    if request is None:
        return None, False
    try:
        text = json.dumps(request, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, False
    if len(text) <= _MAX_REQUEST:
        return text, False

    trimmed = _trim_request_structurally(request)
    if trimmed is None:
        return None, True
    try:
        text2 = json.dumps(trimmed, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, True
    if len(text2) <= _MAX_REQUEST:
        return text2, True
    return None, True



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
        """为旧库补齐新增列与索引。"""
        with self._lock:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
            if "upstream_model" not in cols:
                self._conn.execute("ALTER TABLE requests ADD COLUMN upstream_model TEXT")
            if "truncated" not in cols:
                self._conn.execute("ALTER TABLE requests ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0")
            if "logical_id" not in cols:
                self._conn.execute("ALTER TABLE requests ADD COLUMN logical_id TEXT NOT NULL DEFAULT ''")
            if "client" not in cols:
                self._conn.execute("ALTER TABLE requests ADD COLUMN client TEXT NOT NULL DEFAULT ''")
            try:
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_client_ts ON requests(client, ts)")
            except Exception:
                pass
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
        client: str = "",
        logical_id: str = "",
    ) -> None:
        req_json, req_truncated = _encode_request(request)
        resp_json, truncated = _encode_response(response)
        truncated = truncated or req_truncated
        sql = (
            "INSERT INTO requests(req_id,logical_id,ts,endpoint,model,provider,key_label,client,status,"
            "status_code,latency_ms,request_json,response_json,error,upstream_model,truncated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            req_id, logical_id, time.time(), endpoint, model, provider, key_label, client, status,
            status_code, latency_ms, req_json, resp_json, error, upstream_model,
            int(truncated),
        )
        if self._writer is not None:
            self._writer.enqueue(sql, params)
            return
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        model: str | None = None,
        status: str | None = None,
        client: str | None = None,
        status_code: int | None = None,
        since: float | None = None,
    ) -> list[dict]:
        where: list[str] = []
        params: list = []
        if model:
            where.append("model = ?")
            params.append(model)
        if status:
            where.append("status = ?")
            params.append(status)
        if client:
            where.append("client = ?")
            params.append(client)
        if status_code is not None:
            where.append("status_code = ?")
            params.append(status_code)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        sql = (
            "SELECT req_id,ts,endpoint,model,provider,key_label,client,status,status_code,latency_ms "
            "FROM requests"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        if self._writer is not None:
            rows = self._writer.query(sql, tuple(params))
        else:
            with self._lock:
                rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "req_id": r[0],
                "ts": r[1],
                "endpoint": r[2],
                "model": r[3],
                "provider": r[4],
                "key": r[5],
                "client": r[6],
                "status": r[7],
                "status_code": r[8],
                "latency_ms": r[9],
            }
            for r in rows
        ]

    def get(self, req_id: str) -> dict | None:
        sql = (
            "SELECT req_id,logical_id,ts,endpoint,model,provider,key_label,client,status,status_code,latency_ms,"
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
            "logical_id": row[1],
            "ts": row[2],
            "endpoint": row[3],
            "model": row[4],
            "provider": row[5],
            "key": row[6],
            "client": row[7],
            "status": row[8],
            "status_code": row[9],
            "latency_ms": row[10],
            "request": json.loads(row[11]) if row[11] else None,
            "response": json.loads(row[12]) if row[12] else None,
            "error": row[13],
            "upstream_model": row[14],
            "truncated": bool(row[15]),
        }

    def count_requests(self, client: str | None = None, since: float | None = None) -> int:
        """统计逻辑请求数（按 logical_id 去重，重试共享同一 logical_id 只算 1 次）。"""
        where = ["logical_id != ''"]
        params: list = []
        if client:
            where.append("client = ?")
            params.append(client)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT COUNT(DISTINCT logical_id) FROM requests WHERE " + " AND ".join(where)
        if self._writer is not None:
            return int(self._writer.query_one(sql, tuple(params))[0])
        with self._lock:
            return int(self._conn.execute(sql, tuple(params)).fetchone()[0])

    def close(self) -> None:
        if self._writer is None:
            with self._lock:
                self._conn.close()
