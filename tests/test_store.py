"""SQLite 持久化单元测试。"""

from polygate.store import UsageStore


def test_store_record_and_daily_summary(tmp_path):
    db = tmp_path / "t.db"
    s = UsageStore(str(db))
    s.record("m1", "p1", "ok", prompt=10, completion=5, latency_ms=100.0)
    s.record("m1", "p1", "error")
    s.record("m2", "p1", "ok", prompt=2, completion=3)

    assert s.total_calls() == 3

    days = s.daily_summary(7)
    assert len(days) == 1  # 全部落在今天
    d = days[0]
    assert d["calls"] == 3
    assert d["errors"] == 1
    assert d["prompt_tokens"] == 12
    assert d["completion_tokens"] == 8
    s.close()
