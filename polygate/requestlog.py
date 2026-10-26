"""请求日志与重放（SQLite 持久化）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

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
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""

_MAX_RESPONSE = 20_000


class RequestLog:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
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
    ) -> None:
        req_json = json.dumps(request, ensure_ascii=False) if request is not None else None
        resp_json = json.dumps(response, ensure_ascii=False) if response is not None else None
        if resp_json and len(resp_json) > _MAX_RESPONSE:
            resp_json = resp_json[:_MAX_RESPONSE]
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests(req_id,ts,endpoint,model,provider,key_label,status,status_code,latency_ms,request_json,response_json,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    req_id,
                    time.time(),
                    endpoint,
                    model,
                    provider,
                    key_label,
                    status,
                    status_code,
                    latency_ms,
                    req_json,
                    resp_json,
                    error,
                ),
            )
            self._conn.commit()

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT req_id,ts,endpoint,model,provider,key_label,status,status_code,latency_ms "
                "FROM requests ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
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
        with self._lock:
            row = self._conn.execute(
                "SELECT req_id,ts,endpoint,model,provider,key_label,status,status_code,latency_ms,request_json,response_json,error "
                "FROM requests WHERE req_id=?",
                (req_id,),
            ).fetchone()
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
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
