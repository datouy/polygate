"""共享 SQLite 连接与后台批量写入。

把 UsageStore / RequestLog / CostTracker 的写操作收敛到一个连接、一个后台写线程：
写操作入队即返回，后台批量 ``execute`` 提交，避免同步 SQLite 阻塞事件循环；
统一 WAL + busy_timeout，消除多连接写锁竞争。读路径通过 ``flush()`` 等待队列
排空，保证读到最新已提交数据。
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from collections.abc import Iterable

_SENTINEL = object()
_MAX_QUEUE = 100_000  # 写入队列上限：超过后丢弃写入，避免磁盘异常时内存无界增长
_LOG = logging.getLogger(__name__)


class BatchWriter:
    """单连接 + 后台批量写。所有写操作通过 ``enqueue`` 排队，由后台线程批量提交。"""

    def __init__(self, db_path: str):
        # isolation_level=None 走显式 BEGIN/COMMIT，避免 sqlite3 隐式事务与手动 BEGIN 混用
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE)
        self._stop = threading.Event()
        self._closed = False
        self._dropped = 0
        self._failed = 0
        self._written = 0
        self._flush_timeouts = 0
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="polygate-sqlite", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        """因队列满被丢弃的写入条数。"""
        with self._stats_lock:
            return self._dropped

    def snapshot(self) -> dict:
        with self._stats_lock:
            return {
                "queued": self._queue.qsize(), "written": self._written,
                "dropped": self._dropped, "failed": self._failed,
                "flush_timeouts": self._flush_timeouts, "last_error": self._last_error,
            }

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

    def enqueue(self, sql: str, params: tuple = ()) -> bool:
        if self._closed:
            with self._stats_lock:
                self._dropped += 1
            return False
        try:
            self._queue.put_nowait((sql, params))
            return True
        except queue.Full:
            with self._stats_lock:
                self._dropped += 1
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """阻塞直到当前已入队的写操作全部落库（读路径一致性保证）。"""
        if self._closed:
            return False
        evt = threading.Event()
        result = [False]
        try:
            self._queue.put((_SENTINEL, evt, result), timeout=timeout)
        except queue.Full:
            with self._stats_lock:
                self._flush_timeouts += 1
            return False
        if not evt.wait(timeout):
            with self._stats_lock:
                self._flush_timeouts += 1
            return False
        return result[0]

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            batch: list[tuple] = []
            barriers: list[tuple] = []
            while True:
                if item[0] is _SENTINEL:
                    barriers.append((item[1], item[2]))
                else:
                    batch.append(item)
                # 屏障之前的记录全部提交后，才能释放读线程。
                if barriers or self._stop.is_set():
                    break
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
            success = True
            if batch:
                written, failed, last_error = self._flush_recover(batch)
                with self._stats_lock:
                    self._written += written
                    self._failed += failed
                    if failed:
                        self._last_error = last_error
                if failed:
                    success = False
                    _LOG.error(
                        "SQLite batch write: %d ok, %d failed (last: %s)",
                        written, failed, last_error,
                    )
            for evt, result in barriers:
                result[0] = success
                evt.set()

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

    def _flush_recover(self, batch: list[tuple]) -> tuple[int, int, str]:
        """提交一批写操作；整批失败后逐条重试，隔离真正失败的那几条。

        先走快路径整批事务提交；一旦某条 SQL 触发异常（磁盘满 / 约束冲突 /
        表损坏等），回滚整批后逐条提交，成功条数照常落库，失败条数精确计数，
        而不是把整批一起丢弃。
        """
        try:
            self._flush(batch)
            return (len(batch), 0, "")
        except Exception as first_exc:
            last_error = str(first_exc)[:500]

        written = 0
        failed = 0
        for sql, params in batch:
            try:
                with self._lock:
                    self._conn.execute("BEGIN")
                    self._conn.execute(sql, params)
                    self._conn.commit()
                written += 1
            except Exception as exc:
                failed += 1
                last_error = str(exc)[:500]
                try:
                    with self._lock:
                        self._conn.rollback()
                except Exception:
                    pass
        return written, failed, last_error

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.flush()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> tuple | None:
        self.flush()
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join()
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
