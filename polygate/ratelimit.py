"""令牌桶限流（按 Provider + Key 维度）。"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            return False


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, rate: float, burst: float) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(rate, burst)
                self._buckets[key] = bucket
        return bucket.allow()
