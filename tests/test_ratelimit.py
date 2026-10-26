"""令牌桶限流单元测试。"""

import time

from polygate.ratelimit import RateLimiter, TokenBucket


def test_token_bucket_burst_and_refill():
    b = TokenBucket(rate=1000.0, burst=2.0)
    assert b.allow()
    assert b.allow()
    assert not b.allow()          # burst 耗尽
    time.sleep(0.005)             # 补充 ~5 个 token，但被 burst 封顶为 2
    assert b.allow()


def test_rate_limiter_isolated_keys():
    rl = RateLimiter()
    assert rl.allow("k1", rate=1.0, burst=1.0)
    assert not rl.allow("k1", rate=1.0, burst=1.0)   # 同一 key 桶已空
    assert rl.allow("k2", rate=1.0, burst=1.0)       # 不同 key 独立
