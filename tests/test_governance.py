"""虚拟 Key / 配额 / 预算 / 告警 / 日志过滤单元测试。"""

import time

from polygate.alerts import Alerter
from polygate.clients import ClientPolicy, QuotaChecker, parse_client_keys
from polygate.cost import CostTracker
from polygate.requestlog import RequestLog


def test_parse_client_keys():
    cfg = {
        "client_keys": [
            {
                "key": "sk-biz-a",
                "name": "biz-a",
                "models": ["m1"],
                "rpm": 60,
                "daily_calls": 10,
                "monthly_calls": 100,
                "monthly_budget": 5.0,
            },
            {"key": "  "},  # 空 key 跳过
            "not-a-dict",  # 非法条目跳过
        ]
    }
    out = parse_client_keys(cfg)
    assert set(out) == {"sk-biz-a"}
    pol = out["sk-biz-a"]
    assert pol.name == "biz-a"
    assert pol.models == {"m1"}
    assert pol.rpm == 60
    assert pol.daily_calls == 10
    assert pol.monthly_budget == 5.0
    assert parse_client_keys({}) == {}
    assert parse_client_keys({"client_keys": "bad"}) == {}


def test_quota_model_whitelist_and_rpm():
    checker = QuotaChecker()
    pol = ClientPolicy(name="a", models={"m1"}, rpm=60)
    assert checker.check_model(pol, "m1") is None
    assert "not allowed" in (checker.check_model(pol, "m2") or "")
    # RPM：burst 默认取 rpm，第一次应通过
    assert checker.check_rpm(pol, "a") is None


def test_quota_daily_monthly_budget(tmp_path):
    rl = RequestLog(str(tmp_path / "r.db"))
    cost = CostTracker(str(tmp_path / "c.db"))
    checker = QuotaChecker(store=rl, cost=cost)
    pol = ClientPolicy(name="a", daily_calls=1, monthly_calls=2, monthly_budget=1.0)
    # 空库：全部通过
    assert checker.check_daily(pol, "a") is None
    assert checker.check_monthly_calls(pol, "a") is None
    assert checker.check_budget(pol, "a") is None
    # 写 1 条逻辑请求 → 日配额耗尽
    rl.log(
        req_id="r1",
        endpoint="/v1/chat/completions",
        model="m",
        provider="p",
        key_label="k",
        status="ok",
        client="a",
        logical_id="l1",
    )
    assert "quota" in (checker.check_daily(pol, "a") or "")
    # check_all 返回第一条拒绝原因
    assert checker.check_all(pol, "a", "m") is not None
    # usage 展示用量
    u = checker.usage(pol, "a")
    assert u["daily_calls"]["used"] == 1
    # 预算：花超 1.0 即拒绝
    cost.add("k", "m", 1_000_000, 0, 2.0, client="a")
    assert "budget" in (checker.check_budget(pol, "a") or "")
    rl.close()
    cost.close()


def test_quota_without_store_is_not_blocking():
    checker = QuotaChecker(store=None, cost=None)
    pol = ClientPolicy(name="a", daily_calls=1, monthly_calls=1, monthly_budget=1.0)
    assert checker.check_daily(pol, "a") is None
    assert checker.check_monthly_calls(pol, "a") is None
    assert checker.check_budget(pol, "a") is None
    assert checker.usage(pol, "a") == {}


def test_alerter_disabled_is_noop_and_cooldown():
    a = Alerter(webhook_url=None)
    assert a.enabled is False
    a.notify("k", "ev", "msg")  # 不抛异常
    assert a.snapshot()["enabled"] is False

    b = Alerter(webhook_url="http://example.invalid/hook", cooldown=60)
    assert b.enabled is True
    # 无运行中事件循环时：记录去重但跳过发送，不抛异常
    b.notify("k1", "quota_exceeded", "over")
    assert b.snapshot()["muted_events"] == 1
    b.notify("k1", "quota_exceeded", "over")  # 冷却内去重
    assert b.snapshot()["muted_events"] == 1
    b.notify("k2", "quota_exceeded", "over")
    assert b.snapshot()["muted_events"] == 2


def test_requestlog_filters_and_count(tmp_path):
    rl = RequestLog(str(tmp_path / "r.db"))
    rl.log(
        req_id="a",
        endpoint="/v1/chat/completions",
        model="m1",
        provider="p",
        key_label="k",
        status="ok",
        status_code=200,
        client="alice",
        logical_id="l1",
    )
    rl.log(
        req_id="b",
        endpoint="/v1/chat/completions",
        model="m1",
        provider="p",
        key_label="k",
        status="error",
        status_code=500,
        client="bob",
        logical_id="l2",
    )
    # 重试产生的两条尝试共享同一 logical_id → 配额计数只算 1 次
    rl.log(
        req_id="c1",
        endpoint="/v1/chat/completions",
        model="m2",
        provider="p",
        key_label="k",
        status="error",
        status_code=502,
        client="alice",
        logical_id="shared",
    )
    rl.log(
        req_id="c2",
        endpoint="/v1/chat/completions",
        model="m2",
        provider="p",
        key_label="k",
        status="ok",
        status_code=200,
        client="alice",
        logical_id="shared",
    )
    assert len(rl.list(model="m1")) == 2
    assert len(rl.list(status="error")) == 2
    assert len(rl.list(client="bob")) == 1
    assert len(rl.list(status_code=200)) == 2
    assert len(rl.list(since=time.time() + 3600)) == 0
    assert rl.count_requests(client="alice") == 2  # l1 + shared(去重)
    assert rl.count_requests() == 3  # l1 + l2 + shared(去重)
    assert rl.get("c1")["logical_id"] == "shared"
    rl.close()


def test_cost_summary_by_client(tmp_path):
    c = CostTracker(str(tmp_path / "c.db"))
    c.add("k1", "m", 100, 100, 1.0, client="alice")
    c.add("k1", "m", 100, 100, 2.0, client="bob")
    s = c.summary()
    by_client = {r["client"]: r["cost"] for r in s["by_client"]}
    assert by_client["alice"] == 1.0
    assert by_client["bob"] == 2.0
    assert c.spend_since(client="alice") == 1.0
    assert c.spend_since() == 3.0
    c.close()
