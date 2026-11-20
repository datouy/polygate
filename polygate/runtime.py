"""运行时状态：可从配置构建，并支持热重载（管理面板改配置后立即生效）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .alerts import Alerter
from .circuit import CircuitBreaker, KeyBreaker
from .clients import ClientPolicy, parse_client_keys
from .providers import Provider
from .ratelimit import RateLimiter
from .redislimiter import RedisRateLimiter
from .router import Route, Router
from .virtualkeys import ManagedClientKey, hash_client_key


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

    def capability_set(value) -> set[str]:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item).strip().lower() for item in value if str(item).strip()}

    models: dict[str, list[Route]] = {}
    model_kinds: dict[str, str] = {}
    for m in (cfg.get("models") or []):
        name = m.get("name")
        routes = [
            Route(
                provider=r.get("provider"),
                model=r.get("model"),
                weight=float(r.get("weight", 1.0)),
                capabilities=frozenset(
                    capability_set(m.get("capabilities")) | capability_set(r.get("capabilities"))
                ),
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
    """原子发布完整配置快照；单个请求不会混用两个配置版本。"""

    @dataclass(frozen=True)
    class Snapshot:
        providers: dict
        models: dict
        model_kinds: dict
        rate_limits: dict
        pricing: dict
        budget: dict
        router: Router
        health_enabled: bool
        health_interval: float
        circuits: dict
        key_breakers: dict
        gateway_key: str | None
        admin_key: str | None
        client_keys: dict
        alerter: Alerter | None
        limiter: object
        stream_include_usage: bool
        log_retention_days: float
        total_timeout: float
        max_attempts: int
        retry_backoff: float

    def __init__(self, cfg: dict, managed_client_keys: list[ManagedClientKey] | None = None):
        self._redis_cfg_signature: object = object()
        self.reload(cfg, managed_client_keys)

    def __getattr__(self, name):
        # Reads outside a request (health loop, admin) always use one snapshot.
        if name.startswith("_") or name in {"current", "reload", "editable"}:
            raise AttributeError(name)
        return getattr(self.current, name)

    def reload(self, cfg: dict, managed_client_keys: list[ManagedClientKey] | None = None) -> None:
        previous = getattr(self, "current", None)
        old_circuits = previous.circuits if previous else {}
        old_key_breakers = previous.key_breakers if previous else {}
        old_limiter = previous.limiter if previous else None
        old_alerter = previous.alerter if previous else None
        (providers, models, model_kinds, rate_limits, pricing) = build(cfg)
        raw_budget = cfg.get("budget") or {}
        if not isinstance(raw_budget, dict):
            raise ValueError("budget must be an object")
        try:
            budget = {
                "default_max_output_tokens": int(raw_budget.get("default_max_output_tokens", 4096)),
                "bytes_per_token": float(raw_budget.get("bytes_per_token", 2.0)),
                "reservation_ttl": float(raw_budget.get("reservation_ttl", 900.0)),
            }
        except (TypeError, ValueError):
            raise ValueError(
                "budget.default_max_output_tokens/bytes_per_token/reservation_ttl must be numeric"
            ) from None
        if (
            budget["default_max_output_tokens"] <= 0
            or budget["bytes_per_token"] <= 0
            or budget["reservation_ttl"] <= 0
            or not all(math.isfinite(budget[key]) for key in ("bytes_per_token", "reservation_ttl"))
        ):
            raise ValueError("budget values must be positive")
        router = Router(providers, models)

        health_cfg = cfg.get("health") or {}
        health_enabled = bool(health_cfg.get("enabled", True))
        health_interval = float(health_cfg.get("interval", 30.0))
        threshold = int(health_cfg.get("failure_threshold", 3))
        cooldown = float(health_cfg.get("cooldown", 60.0))

        # 保留已有熔断器实例，仅更新参数；新增 Provider 才新建，移除的随之丢弃。
        # 否则每次保存配置都会把失败计数归零，熔断器在生产中形同虚设。
        circuits: dict[str, CircuitBreaker] = {}
        for name in providers:
            cb = old_circuits.get(name)
            if cb is None:
                cb = CircuitBreaker(failure_threshold=threshold, cooldown_sec=cooldown)
            else:
                cb.failure_threshold = threshold
                cb.cooldown_sec = cooldown
            circuits[name] = cb

        # Key 级熔断器：认证失效（401/403）的 Key 短期内不再使用。
        # 保留已有实例，仅新增/移除 Key，避免每次热重载把失败计数归零。
        key_threshold = int(health_cfg.get("key_failure_threshold", 2))
        key_cooldown = float(health_cfg.get("key_cooldown", 300.0))
        key_breakers: dict[str, dict[str, KeyBreaker]] = {}
        for name, prov in providers.items():
            existing = old_key_breakers.get(name, {})
            per_provider: dict[str, KeyBreaker] = {}
            for key in prov.api_keys:
                kb = existing.get(key)
                if kb is None:
                    kb = KeyBreaker(failure_threshold=key_threshold, cooldown_sec=key_cooldown)
                else:
                    kb.failure_threshold = key_threshold
                    kb.cooldown_sec = key_cooldown
                per_provider[key] = kb
            key_breakers[name] = per_provider
        auth = cfg.get("auth") or {}
        gateway_key = auth.get("api_key") or None
        admin_key = auth.get("admin_key") or gateway_key
        client_keys: dict[str, ClientPolicy] = {}
        for raw_key, policy in parse_client_keys(auth).items():
            policy.key_prefix = policy.key_prefix or mask_key(raw_key)
            client_keys[hash_client_key(raw_key)] = policy
        for record in managed_client_keys or []:
            if record.revoked_at:
                continue
            client_keys[record.key_hash] = record.policy()

        # 告警：仅在 webhook 变化时重建，避免热重载重复创建/丢连接。
        alert_cfg = cfg.get("alerting") or {}
        webhook = str(alert_cfg.get("webhook_url") or "").strip()
        if webhook:
            if old_alerter is None or old_alerter.webhook_url != webhook:
                alerter = Alerter(
                    webhook,
                    cooldown=float(alert_cfg.get("cooldown", 300.0)),
                    timeout=float(alert_cfg.get("timeout", 5.0)),
                )
            else:
                alerter = old_alerter
        else:
            alerter = None

        # 仅当 Redis URL 变化时才重建限流器，避免每次热重载泄漏一个 Redis 连接池。
        redis_cfg = cfg.get("redis")
        sig = (redis_cfg or {}).get("url") if isinstance(redis_cfg, dict) else None
        limiter = old_limiter
        if limiter is None or sig != self._redis_cfg_signature:
            self._redis_cfg_signature = sig
            limiter = build_limiter(redis_cfg)
            if old_limiter is not None and hasattr(old_limiter, "close"):
                old_limiter.close()

        # stream_options.include_usage 自动注入开关（部分国产厂商不兼容，可关）。
        stream_include_usage = bool(cfg.get("stream_include_usage", True))

        # 日志保留天数（0 = 永久保留）。>0 时会定期清理 calls/requests/costs 中过旧数据。
        log_retention_days = float(cfg.get("log_retention_days", 0) or 0)
        limits = cfg.get("limits") or {}
        total_timeout = float(limits.get("total_timeout", 180.0))
        max_attempts = int(limits.get("max_attempts", 8))
        retry_backoff = float(limits.get("retry_backoff", 0.15))
        if total_timeout <= 0 or max_attempts <= 0 or retry_backoff < 0:
            raise ValueError("limits.total_timeout/max_attempts must be positive; retry_backoff >= 0")
        # Publishing this single reference is the only mutation visible to
        # requests.  A failed build above leaves the previous snapshot intact.
        self.current = self.Snapshot(
            providers=providers, models=models, model_kinds=model_kinds,
            rate_limits=rate_limits, pricing=pricing, budget=budget, router=router,
            health_enabled=health_enabled, health_interval=health_interval,
            circuits=circuits, key_breakers=key_breakers, gateway_key=gateway_key,
            admin_key=admin_key, client_keys=client_keys,
            alerter=alerter, limiter=limiter, stream_include_usage=stream_include_usage,
            log_retention_days=log_retention_days, total_timeout=total_timeout,
            max_attempts=max_attempts, retry_backoff=retry_backoff,
        )

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
                        {
                            "provider": r.provider, "model": r.model,
                            "weight": r.weight, "capabilities": sorted(r.capabilities),
                        }
                        for r in routes
                    ],
                }
                for name, routes in self.models.items()
            ],
            "budget": dict(self.budget),
            "presets": {
                "deepseek": "https://api.deepseek.com",
                "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                "moonshot": "https://api.moonshot.cn/v1",
                "openai": "https://api.openai.com/v1",
            },
        }
