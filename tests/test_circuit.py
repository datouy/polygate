"""熔断器单元测试。"""

import time

from polygate.circuit import CircuitBreaker


def test_circuit_trip_and_recover():
    c = CircuitBreaker(failure_threshold=2, cooldown_sec=0.05)
    assert c.allow()            # closed -> 放行
    c.record_failure()
    assert c.state == "closed"  # 未到阈值
    c.record_failure()
    assert c.state == "open"    # 触发熔断

    assert not c.allow()        # open 未冷却 -> 阻塞

    time.sleep(0.06)
    assert c.allow()            # 冷却结束 -> half_open 放行单个探测
    assert c.state == "half_open"
    assert not c.allow()        # 探测在途 -> 阻塞后续

    c.record_success()          # 探测成功 -> 关闭熔断
    assert c.state == "closed"
    assert c.allow()


def test_circuit_reopens_after_failed_probe():
    c = CircuitBreaker(failure_threshold=1, cooldown_sec=0.03)
    c.record_failure()          # 直接 open
    assert c.state == "open"
    time.sleep(0.04)
    assert c.allow()            # half_open 探测
    c.record_failure()          # 探测失败 -> 再次 open
    assert c.state == "open"
    assert not c.allow()


def test_probe_does_not_reset_business_failures():
    """健康探测成功不得清零 closed 状态下的业务失败计数。"""
    c = CircuitBreaker(failure_threshold=3, cooldown_sec=0.05)
    c.record_failure()          # 业务失败 1
    assert c.snapshot()["consecutive_failures"] == 1
    c.record_probe(True)        # 健康探测成功
    assert c.snapshot()["consecutive_failures"] == 1  # 不被清零
    assert c.state == "closed"
    c.record_failure()          # 业务失败 2
    c.record_failure()          # 业务失败 3 -> 熔断
    assert c.state == "open"


def test_probe_recovers_open_circuit():
    """主动健康探测成功应能把 open 状态的熔断器闭合。

    否则会形成死锁：熔断 → 业务请求全部跳过该 Provider → 没有探测流量 →
    永远不恢复，上游即使已恢复也只能靠重启网关。
    """
    c = CircuitBreaker(failure_threshold=1, cooldown_sec=0.03)
    c.record_failure()          # open
    assert c.state == "open"
    c.record_probe(True)        # 探测成功 → 直接闭合
    assert c.state == "closed"
    assert c.snapshot()["consecutive_failures"] == 0


def test_probe_recovers_half_open():
    c = CircuitBreaker(failure_threshold=1, cooldown_sec=0.03)
    c.record_failure()          # open
    time.sleep(0.04)
    c.allow()                   # half_open
    c.record_probe(True)        # half_open 下探测成功恢复
    assert c.state == "closed"
    assert c.snapshot()["consecutive_failures"] == 0


def test_probe_failure_keeps_open():
    """探测失败不应把熔断器误关。"""
    c = CircuitBreaker(failure_threshold=1, cooldown_sec=0.03)
    c.record_failure()
    assert c.state == "open"
    c.record_probe(False)
    assert c.state == "open"
