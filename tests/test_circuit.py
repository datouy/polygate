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
