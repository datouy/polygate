"""Redis 分布式限流测试（真实 Redis 不可用时自动跳过；另含 fail-open 降级测试）。"""

import pytest


def _redis_available() -> bool:
    try:
        import redis

        r = redis.Redis.from_url("redis://127.0.0.1:6379/0", protocol=2)
        r.ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="redis 不可用")
def test_redis_rate_limiter():
    from polygate.redislimiter import RedisRateLimiter

    rl = RedisRateLimiter("redis://127.0.0.1:6379/0")
    key = f"polygate-test:{id(object())}"
    # burst=1、极低 rate：第一次放行，第二次被限
    assert rl.allow(key, 0.01, 1.0) is True
    assert rl.allow(key, 0.01, 1.0) is False


def test_redis_rate_limiter_fail_open():
    """连接不可达时 fail-open（放行），保证网关可用性优先。"""
    from polygate.redislimiter import RedisRateLimiter

    rl = RedisRateLimiter("redis://127.0.0.1:1/0")  # 端口无服务
    assert rl.allow("k", 10.0, 1.0) is True
