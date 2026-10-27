"""共享 SQLite 连接与后台批量写入。

把 UsageStore / RequestLog / CostTracker 的写操作收敛到一个连接、一个后台写线程：
写操作入队即返回，后台批量 ``execute`` 提交，避免同步 SQLite 阻塞事件循环；
统一 WAL + busy_timeout，消除多连接写锁竞争。读路径通过 ``flush()`` 等待队列
排空，保证读到最新已提交数据。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from typing import Iterable

_SENTINEL = object()


class BatchWriter:
    """单连接 + 后台批量写。所有写操作通过 ``enqueue`` 排队，由后台线程批量提交。"""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def executescript(self, script: str) -> None:
        """启动期建表（同步，一次性）。"""
        with self._lock:
            self._conn.executescript(script)
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        """启动期一次性同步写。"""
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def enqueue(self, sql: str, params: tuple = ()) -> None:
        self._queue.put((sql, params))

    def flush(self, timeout: float = 5.0) -> None:
        """阻塞直到当前已入队的写操作全部落库（读路径一致性保证）。"""
        evt = threading.Event()
        self._queue.put((_SENTINEL, evt))
        evt.wait(timeout=timeout)

    def _run(self) -> None:
        self._started.set()
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item[0] is _SENTINEL:  # 屏障：前面的写已全部落库
                item[1].set()
                continue
            batch: list[tuple] = [item]
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt[0] is _SENTINEL:
                    nxt[1].set()
                    continue
                batch.append(nxt)
            if batch:
                self._flush(batch)

    def _flush(self, batch: Iterable[tuple]) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                for sql, params in batch:
                    self._conn.execute(sql, params)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.flush()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> tuple | None:
        self.flush()
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        pending: list[tuple] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item[0] is _SENTINEL:
                continue
            pending.append(item)
        if pending:
            self._flush(pending)
        with self._lock:
            self._conn.close()
