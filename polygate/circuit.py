"""熔断器（Circuit Breaker）：closed / open / half_open 三态。

同时提供一个 Key 级别的熔断器：某个 API Key 失效（401/403）后，
短期内不再把请求发给它，避免每次请求白白多付一次 401 的 RTT。
"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """Provider 级熔断器。

    状态机::

        closed --(连续失败 >= threshold)--> open
        open   --(冷却期结束，业务请求探测)--> half_open
        half_open --(探测成功)--> closed
        half_open --(探测失败)--> open

    主动健康探测（``record_probe``）在 **open** 状态下也能把熔断器闭合：
    上游恢复后不应必须等真实业务请求撞上来才恢复，否则「熔断 → 跳过该 Provider
    → 永不产生探测流量 → 永远不恢复」会形成死锁。
    """

    def __init__(self, failure_threshold: int = 3, cooldown_sec: float = 30.0):
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.consecutive_failures = 0
        self.state = "closed"
        self.opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """是否允许放行一个请求。

        closed 直接放行；open 在冷却期结束后转 half_open 放行单个探测；
        half_open 期间（探测在途）不再放行，直到探测成功/失败后状态迁移。
        """
        with self._lock:
            now = time.monotonic()
            if self.state == "closed":
                return True
            if self.state == "open":
                if now - self.opened_at >= self.cooldown_sec:
                    self.state = "half_open"
                    return True
                return False
            # half_open：探测在途，阻塞后续请求
            return False

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = time.monotonic()

    def record_probe(self, success: bool) -> None:
        """记录主动健康探测的结果。

        与业务流量（record_success/record_failure）分开：探测 **不会** 重置
        closed 状态下的业务失败计数（熔断应由真实请求流量驱动）。

        但探测 **可以** 关闭一个处于 open 状态的熔断器：这是上游已恢复的
        唯一信号来源，若不允许 open → closed 的迁移，熔断将无法自动恢复。
        """
        with self._lock:
            if success:
                if self.state in ("open", "half_open"):
                    self.consecutive_failures = 0
                    self.state = "closed"
                return
            # 探测失败：仅在 half_open 时重新计时；closed 状态下不因探测而熔断
            if self.state == "half_open":
                self.state = "open"
                self.opened_at = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "consecutive_failures": self.consecutive_failures}


class KeyBreaker:
    """Key 级熔断器：某个 Key 连续认证失败后，短期内不再使用。

    与 Provider 级熔断器的区别：
    * Key 熔断只统计 401/403（认证维度故障），不统计 5xx。
    * 打开后冷却一段时间自动恢复，用于应对「Key 过期/额度被撤销/配置写错」。
    """

    def __init__(self, failure_threshold: int = 2, cooldown_sec: float = 300.0):
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.consecutive_failures < self.failure_threshold:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown_sec:
                # 冷却结束，放行一个探测；成功则 record_success 会清零
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self.opened_at = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": "open" if self.consecutive_failures >= self.failure_threshold else "closed",
                "consecutive_failures": self.consecutive_failures,
            }
