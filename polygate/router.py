"""模型路由：按权重选 Provider、多 Key 轮询、生成故障切换候选序列。"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from .providers import Provider


@dataclass
class Route:
    provider: str
    model: str
    weight: float = 1.0


@dataclass
class Candidate:
    provider_name: str
    provider: Provider
    api_key: str
    upstream_model: str


class Router:
    def __init__(self, providers: dict[str, Provider], models: dict[str, list[Route]]):
        self.providers = providers
        self.models = models
        self._offsets: dict[str, int] = {}
        self._lock = threading.Lock()

    def routes_for(self, model: str) -> list[Route]:
        return self.models.get(model, [])

    def candidates(self, model: str) -> list[Candidate]:
        """返回按优先级排序的候选列表：优先命中权重选中的 Provider，其余作为故障切换备选。"""
        routes = self.models.get(model)
        if not routes:
            return []

        picked = self._weighted_pick(routes)
        ordered = [picked] + [r for r in routes if r is not picked]

        out: list[Candidate] = []
        for route in ordered:
            prov = self.providers.get(route.provider)
            if prov is None or not prov.api_keys:
                continue
            offset = self._next_offset(prov.name, len(prov.api_keys))
            for i in range(len(prov.api_keys)):
                key = prov.api_keys[(offset + i) % len(prov.api_keys)]
                out.append(Candidate(prov.name, prov, key, route.model))
        return out

    def _weighted_pick(self, routes: list[Route]) -> Route:
        total = sum(r.weight for r in routes) or float(len(routes))
        r = random.uniform(0.0, total)
        acc = 0.0
        for route in routes:
            acc += route.weight
            if r <= acc:
                return route
        return routes[-1]

    def _next_offset(self, provider: str, n: int) -> int:
        with self._lock:
            cur = self._offsets.get(provider, 0)
            self._offsets[provider] = cur + 1
            return cur % n
