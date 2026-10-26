"""内存用量统计（按模型 / Provider 维度）。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self.calls: dict[str, int] = defaultdict(int)
        self.errors: dict[str, int] = defaultdict(int)
        self.prompt_tokens: dict[str, int] = defaultdict(int)
        self.completion_tokens: dict[str, int] = defaultdict(int)
        self.total_tokens: dict[str, int] = defaultdict(int)
        self.latency_ms: dict[str, float] = defaultdict(float)
        self.by_provider: dict[str, int] = defaultdict(int)

    def record(
        self,
        model: str,
        provider: str,
        status: str,
        prompt: int = 0,
        completion: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        with self._lock:
            self.calls[model] += 1
            if status == "error":
                self.errors[model] += 1
            self.prompt_tokens[model] += prompt
            self.completion_tokens[model] += completion
            self.total_tokens[model] += prompt + completion
            self.latency_ms[model] += latency_ms
            if provider:
                self.by_provider[provider] += 1

    def snapshot(self) -> dict:
        with self._lock:
            models = {}
            for m, calls in self.calls.items():
                models[m] = {
                    "calls": calls,
                    "errors": self.errors.get(m, 0),
                    "prompt_tokens": self.prompt_tokens.get(m, 0),
                    "completion_tokens": self.completion_tokens.get(m, 0),
                    "total_tokens": self.total_tokens.get(m, 0),
                    "avg_latency_ms": round(self.latency_ms.get(m, 0.0) / max(1, calls), 1),
                }
            return {
                "uptime_seconds": round(time.time() - self._started, 1),
                "models": models,
                "calls_by_provider": dict(self.by_provider),
            }
