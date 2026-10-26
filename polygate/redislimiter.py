"""Redis 分布式令牌桶限流（可选后端）。

使用 Lua 脚本保证原子性；Redis 不可用时 fail-open（放行），避免拖垮网关。
"""

from __future__ import annotations

import time

_TOKEN_BUCKET_SCRIPT = """
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local updated = tonumber(redis.call('HGET', KEYS[1], 'updated'))
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
if tokens == nil then tokens = burst end
if updated == nil then updated = now end
tokens = math.min(burst, tokens + (now - updated) * rate)
local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', KEYS[1], 120)
return allowed
"""


class RedisRateLimiter:
    def __init__(self, url: str):
        import redis  # 延迟导入，未安装 redis 包时不影响其它功能

        self._redis = redis.Redis.from_url(url, decode_responses=False, protocol=2)
        self._script = self._redis.register_script(_TOKEN_BUCKET_SCRIPT)

    def allow(self, key: str, rate: float, burst: float) -> bool:
        try:
            result = self._script(keys=[f"polygate:rl:{key}"], args=[rate, burst, time.time(), 1.0])
            return bool(int(result))
        except Exception:
            # Redis 故障时降级放行，保证网关可用性优先
            return True
