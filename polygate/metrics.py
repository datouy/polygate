"""轻量 Prometheus 指标（无第三方依赖，text exposition 格式）。"""

from __future__ import annotations

import threading
from collections import defaultdict

_LATENCY_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]


def _num(v):
    if isinstance(v, int):
        return str(v)
    f = float(v)
    if f == int(f):
        return str(int(f))
    return repr(f)


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple, float] = defaultdict(float)
        self._labels: dict[tuple, dict] = {}
        self._hist: dict[tuple, dict] = defaultdict(
            lambda: {"buckets": [0] * (len(_LATENCY_BUCKETS) + 1), "count": 0, "sum": 0.0}
        )

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        labels = labels or {}
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value
            self._labels[key] = labels

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        labels = labels or {}
        key = self._key(name, labels)
        with self._lock:
            h = self._hist[key]
            self._labels[key] = labels
            h["count"] += 1
            h["sum"] += value
            placed = False
            for i, ub in enumerate(_LATENCY_BUCKETS):
                if value <= ub:
                    h["buckets"][i] += 1
                    placed = True
                    break
            if not placed:
                h["buckets"][-1] += 1

    def render(self, extra_gauges: list[tuple] | None = None) -> str:
        lines: list[str] = []
        with self._lock:
            for key, v in self._counters.items():
                name = key[0]
                lines.append(self._fmt(name, self._labels.get(key, {}), v))
            for key, h in self._hist.items():
                name = key[0]
                labels = self._labels.get(key, {})
                lines.append(self._fmt(name + "_count", labels, h["count"]))
                lines.append(self._fmt(name + "_sum", labels, h["sum"]))
                cum = 0
                for i, c in enumerate(h["buckets"]):
                    cum += c
                    le = "+Inf" if i == len(_LATENCY_BUCKETS) else str(_LATENCY_BUCKETS[i])
                    lines.append(self._fmt(name + "_bucket", {**labels, "le": le}, cum))
        for name, labels, value in (extra_gauges or []):
            lines.append(self._fmt(name, labels or {}, value))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict) -> tuple:
        return (name, ",".join(f"{k}={v}" for k, v in sorted(labels.items())))

    @staticmethod
    def _fmt(name: str, labels: dict, value) -> str:
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            return f"{name}{{{label_str}}} {_num(value)}"
        return f"{name} {_num(value)}"
