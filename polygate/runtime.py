"""运行时状态：可从配置构建，并支持热重载（管理面板改配置后立即生效）。"""

from __future__ import annotations

from .circuit import CircuitBreaker
from .providers import Provider
from .ratelimit import RateLimiter
from .redislimiter import RedisRateLimiter
from .router import Route, Router


def build(cfg: dict):
    providers: dict[str, Provider] = {}
    for name, p in (cfg.get("providers") or {}).items():
        base_url = p.get("base_url")
        if not base_url:
            continue
        keys = list(p.get("api_keys") or [])
        if p.get("api_key"):
            keys.insert(0, p["api_key"])
        keys = [k for k in keys if k]
        providers[name] = Provider(
            name=name,
            base_url=base_url,
            api_keys=keys,
            timeout=float(p.get("timeout", 120.0)),
        )

    models: dict[str, list[Route]] = {}
    model_kinds: dict[str, str] = {}
    for m in (cfg.get("models") or []):
        name = m.get("name")
        routes = [
            Route(
                provider=r.get("provider"),
                model=r.get("model"),
                weight=float(r.get("weight", 1.0)),
            )
            for r in (m.get("providers") or [])
            if r.get("provider") and r.get("model")
        ]
        if name and routes:
            models[name] = routes
            model_kinds[name] = m.get("kind", "chat")

    rate_limits: dict[str, tuple[float, float]] = {}
    for name, p in (cfg.get("providers") or {}).items():
        rl = p.get("rate_limit")
        if rl:
            rate_limits[name] = (float(rl.get("rate", 10.0)), float(rl.get("burst", 10.0)))

    pricing = cfg.get("pricing") or {}
    return providers, models, model_kinds, rate_limits, pricing


def build_limiter(redis_cfg):
    if not redis_cfg or not redis_cfg.get("url"):
        return RateLimiter()
    try:
        return RedisRateLimiter(redis_cfg["url"])
    except Exception:
        return RateLimiter()


def mask_key(key: str) -> str:
    """把 Key 脱敏，如 c58d...f9db。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


class RuntimeState:
    """网关运行时状态。reload() 会就地更新，所有处理函数实时读到新值。"""

    def __init__(self, cfg: dict):
        self.circuits: dict[str, CircuitBreaker] = {}
        self.limiter = None
        self._redis_cfg_signature: object = object()
        self.reload(cfg)

    def reload(self, cfg: dict) -> None:
        self.cfg = cfg
        (self.providers, self.models, self.model_kinds,
         self.rate_limits, self.pricing) = build(cfg)
        self.router = Router(self.providers, self.models)

        health_cfg = cfg.get("health") or {}
        self.health_enabled = bool(health_cfg.get("enabled", True))
        self.health_interval = float(health_cfg.get("interval", 30.0))
        threshold = int(health_cfg.get("failure_threshold", 3))
        cooldown = float(health_cfg.get("cooldown", 60.0))

        # 保留已有熔断器实例，仅更新参数；新增 Provider 才新建，移除的随之丢弃。
        # 否则每次保存配置都会把失败计数归零，熔断器在生产中形同虚设。
        circuits: dict[str, CircuitBreaker] = {}
        for name in self.providers:
            cb = self.circuits.get(name)
            if cb is None:
                cb = CircuitBreaker(failure_threshold=threshold, cooldown_sec=cooldown)
            else:
                cb.failure_threshold = threshold
                cb.cooldown_sec = cooldown
            circuits[name] = cb
        self.circuits = circuits

        auth = cfg.get("auth") or {}
        self.gateway_key = auth.get("api_key") or None
        self.admin_key = auth.get("admin_key") or self.gateway_key

        # 仅当 Redis URL 变化时才重建限流器，避免每次热重载泄漏一个 Redis 连接池。
        redis_cfg = cfg.get("redis")
        sig = (redis_cfg or {}).get("url") if isinstance(redis_cfg, dict) else None
        if self.limiter is None or sig != self._redis_cfg_signature:
            self._redis_cfg_signature = sig
            self.limiter = build_limiter(redis_cfg)

        # stream_options.include_usage 自动注入开关（部分国产厂商不兼容，可关）。
        self.stream_include_usage = bool(cfg.get("stream_include_usage", True))

    def editable(self) -> dict:
        """给管理面板用的可编辑视图（Key 已脱敏，绝不返回明文）。"""
        return {
            "providers": {
                name: {
                    "base_url": p.base_url,
                    "has_key": bool(p.api_keys),
                    "key_hint": mask_key(p.api_keys[0]) if p.api_keys else "",
                    "key_count": len(p.api_keys),
                    "timeout": p.timeout,
                    "rate_limit": self.rate_limits.get(name),
                }
                for name, p in self.providers.items()
            },
            "models": [
                {
                    "name": name,
                    "kind": self.model_kinds.get(name, "chat"),
                    "routes": [
                        {"provider": r.provider, "model": r.model, "weight": r.weight}
                        for r in routes
                    ],
                }
                for name, routes in self.models.items()
            ],
            "presets": {
                "deepseek": "https://api.deepseek.com",
                "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                "moonshot": "https://api.moonshot.cn/v1",
                "openai": "https://api.openai.com/v1",
            },
        }
