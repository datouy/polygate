"""核心逻辑单元测试（无需网络）。"""

import pytest

from polygate.config import load_config
from polygate.errors import is_client_error, is_key_error, is_rate_limited, is_retryable
from polygate.providers import Provider
from polygate.router import Route, Router
from polygate.stats import Stats


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO_KEY", "secret123")
    p = tmp_path / "c.yaml"
    p.write_text("auth:\n  api_key: ${FOO_KEY}\nmissing: ${NOT_SET}\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="NOT_SET"):
        cfg = load_config(p)
    assert cfg["auth"]["api_key"] == "secret123"
    assert cfg["missing"] == ""


def test_router_candidates_order_and_failover():
    providers = {
        "a": Provider("a", "http://a/v1", ["ka1", "ka2"]),
        "b": Provider("b", "http://b/v1", ["kb1"]),
    }
    models = {"m": [Route("a", "m", 3.0), Route("b", "m", 1.0)]}
    r = Router(providers, models)
    cands = r.candidates("m")
    # a 有 2 个 key + b 有 1 个 key => 共 3 个候选
    assert len(cands) == 3
    assert {c.provider_name for c in cands} == {"a", "b"}
    # 未知模型
    assert r.candidates("nope") == []


def test_router_skips_provider_without_key():
    providers = {
        "a": Provider("a", "http://a/v1", []),
        "b": Provider("b", "http://b/v1", ["kb1"]),
    }
    models = {"m": [Route("a", "m", 3.0), Route("b", "m", 1.0)]}
    r = Router(providers, models)
    cands = r.candidates("m")
    assert len(cands) == 1
    assert cands[0].provider_name == "b"


def test_retryable():
    assert is_retryable(None)
    assert is_retryable(429)
    assert is_retryable(500)
    assert is_retryable(503)
    assert not is_retryable(400)
    assert not is_retryable(401)
    assert not is_retryable(403)
    assert not is_retryable(404)


def test_key_error():
    # 401/403 属于 Key 维度问题：应触发换 Key；其余 4xx 不是 Key 维度问题
    assert is_key_error(401)
    assert is_key_error(403)
    assert not is_key_error(400)
    assert not is_key_error(404)
    assert not is_key_error(422)
    assert not is_key_error(500)
    assert not is_key_error(None)


def test_rate_limited_is_separate_from_key_error():
    """429 是独立的语义：换 Key 有效，但语义上不等于「Key 失效」。

    401/403 表示 Key 无效（应隔离该 Key）；429 表示额度/速率被限
    （Key 可能仍然有效，换一个 Key 或稍后重试即可）。
    """
    assert is_rate_limited(429)
    assert not is_rate_limited(401)
    assert not is_rate_limited(500)
    assert not is_rate_limited(None)
    assert not is_key_error(429)


def test_client_error():
    """4xx（除 401/403/429）应原样回传客户端，而不是降级成 502。"""
    assert is_client_error(400)
    assert is_client_error(404)
    assert is_client_error(422)
    assert not is_client_error(401)
    assert not is_client_error(403)
    assert not is_client_error(429)
    assert not is_client_error(500)
    assert not is_client_error(None)


def test_stats():
    s = Stats()
    s.record("m", "p", "ok", prompt=10, completion=5, latency_ms=120.0)
    s.record("m", "p", "error")
    snap = s.snapshot()
    m = snap["models"]["m"]
    assert m["calls"] == 2
    assert m["errors"] == 1
    assert m["prompt_tokens"] == 10
    assert m["completion_tokens"] == 5
    assert m["total_tokens"] == 15
    assert snap["calls_by_provider"]["p"] == 2


def test_extract_stream_usage():
    from polygate.server import _extract_stream_usage

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    # 普通 delta 行（不含 usage）不应改变计数
    usage = _extract_stream_usage(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n', usage)
    assert usage["total_tokens"] == 0
    # 末尾 usage 行应被解析
    usage = _extract_stream_usage(
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}', usage
    )
    assert usage == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    # [DONE] 与非 data 行忽略
    usage = _extract_stream_usage(b"data: [DONE]", usage)
    usage = _extract_stream_usage(b"", usage)
    assert usage["completion_tokens"] == 3
