"""熔断器（Circuit Breaker）：closed / open / half_open 三态。"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
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

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "consecutive_failures": self.consecutive_failures}
