"""SQLite BatchWriter 屏障、失败隔离与关闭语义测试。"""

from polygate.db import BatchWriter


def test_flush_barrier_returns_true_and_writes_visible(tmp_path):
    w = BatchWriter(str(tmp_path / "w.db"))
    w.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    assert w.enqueue("INSERT INTO t(v) VALUES(?)", ("a",)) is True
    assert w.flush() is True
    assert w.query("SELECT v FROM t ORDER BY id") == [("a",)]
    w.close()


def test_flush_is_fifo_barrier(tmp_path):
    w = BatchWriter(str(tmp_path / "w.db"))
    w.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for v in ("a", "b", "c"):
        w.enqueue("INSERT INTO t(v) VALUES(?)", (v,))
    # flush() 返回 True 意味着屏障之前的写入已全部提交
    assert w.flush() is True
    rows = w.query("SELECT v FROM t ORDER BY id")
    assert [r[0] for r in rows] == ["a", "b", "c"]
    w.close()


def test_failed_batch_isolates_bad_item(tmp_path):
    w = BatchWriter(str(tmp_path / "w.db"))
    w.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("ok1",))
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("dup",))
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("dup",))  # UNIQUE 冲突
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("ok2",))
    # 有一条失败 → flush 返回 False，但其余三条仍应落库
    assert w.flush() is False
    snap = w.snapshot()
    assert snap["written"] == 3
    assert snap["failed"] == 1
    assert snap["last_error"]
    rows = w.query("SELECT v FROM t ORDER BY id")
    assert [r[0] for r in rows] == ["ok1", "dup", "ok2"]
    w.close()


def test_close_drains_pending_writes(tmp_path):
    w = BatchWriter(str(tmp_path / "w.db"))
    w.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("a",))
    w.enqueue("INSERT INTO t(v) VALUES(?)", ("b",))
    w.close()

    # 重新打开验证：close() 后数据没有丢失
    w2 = BatchWriter(str(tmp_path / "w.db"))
    rows = w2.query("SELECT v FROM t ORDER BY id")
    assert [r[0] for r in rows] == ["a", "b"]
    w2.close()


def test_flush_and_enqueue_after_close(tmp_path):
    w = BatchWriter(str(tmp_path / "w.db"))
    w.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    w.close()
    assert w.flush() is False
    assert w.enqueue("INSERT INTO t(v) VALUES(?)", ("x",)) is False
