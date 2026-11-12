"""内存用量统计（按模型 / Provider 维度）。

口径约定（运营看板准确性的关键）：
* ``requests`` —— 逻辑请求数：客户端发起一次 ``/v1/chat/completions`` 调用记 1 次。
* ``attempts`` —— 上游尝试数：一次逻辑请求可能触发多次上游调用
  （多 Key 轮询、故障切换），每次记 1 次。
* 旧字段 ``calls`` 保留为尝试数，保证历史行为与旧测试兼容。

延迟分位：每个模型保留最近 ``_MAX_SAMPLES`` 条延迟样本（有界 deque），
snapshot 时计算 p50 / p95 / p99，供运营判断长尾。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_MAX_SAMPLES = 1024


def _percentile(sorted_samples: list[float], q: float) -> float:
    """nearest-rank 分位。空样本返回 0.0。"""
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return round(sorted_samples[0], 1)
    import math

    rank = math.ceil(q / 100.0 * len(sorted_samples))
    rank = min(max(rank, 1), len(sorted_samples))
    return round(sorted_samples[rank - 1], 1)


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
        # 新增：逻辑请求口径 + 延迟样本
        self.requests: dict[str, int] = defaultdict(int)
        self.request_errors: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
        # 按客户端维度（虚拟 Key）：尝试数与逻辑请求数分开计
        self.calls_by_client: dict[str, int] = defaultdict(int)
        self.errors_by_client: dict[str, int] = defaultdict(int)
        self.requests_by_client: dict[str, int] = defaultdict(int)

    def record(
        self,
        model: str,
        provider: str,
        status: str,
        prompt: int = 0,
        completion: int = 0,
        latency_ms: float = 0.0,
        client: str | None = None,
    ) -> None:
        """记录一次上游尝试（attempt）。"""
        with self._lock:
            self.calls[model] += 1
            if status == "error":
                self.errors[model] += 1
            self.prompt_tokens[model] += prompt
            self.completion_tokens[model] += completion
            self.total_tokens[model] += prompt + completion
            self.latency_ms[model] += latency_ms
            if latency_ms:
                self._latencies[model].append(float(latency_ms))
            if provider:
                self.by_provider[provider] += 1
            if client:
                self.calls_by_client[client] += 1
                if status == "error":
                    self.errors_by_client[client] += 1

    def record_request(self, model: str, status: str, client: str | None = None) -> None:
        """记录一次逻辑请求（request）：无论触发多少次上游尝试，只记 1 次。"""
        with self._lock:
            self.requests[model] += 1
            if status == "error":
                self.request_errors[model] += 1
            if client:
                self.requests_by_client[client] += 1

    def snapshot(self) -> dict:
        with self._lock:
            models = {}
            for m, calls in self.calls.items():
                samples = sorted(self._latencies.get(m, ()))
                models[m] = {
                    # attempt 口径（兼容旧字段）
                    "calls": calls,
                    "errors": self.errors.get(m, 0),
                    # request 口径（运营看板用这个）
                    "requests": self.requests.get(m, 0),
                    "request_errors": self.request_errors.get(m, 0),
                    "prompt_tokens": self.prompt_tokens.get(m, 0),
                    "completion_tokens": self.completion_tokens.get(m, 0),
                    "total_tokens": self.total_tokens.get(m, 0),
                    "avg_latency_ms": round(self.latency_ms.get(m, 0.0) / max(1, calls), 1),
                    "p50_latency_ms": _percentile(samples, 50),
                    "p95_latency_ms": _percentile(samples, 95),
                    "p99_latency_ms": _percentile(samples, 99),
                }
            return {
                "uptime_seconds": round(time.time() - self._started, 1),
                "models": models,
                "calls_by_provider": dict(self.by_provider),
                "calls_by_client": dict(self.calls_by_client),
                "errors_by_client": dict(self.errors_by_client),
            }
