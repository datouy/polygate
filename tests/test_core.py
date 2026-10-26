"""核心逻辑单元测试（无需网络）。"""

from polygate.config import load_config
from polygate.errors import is_retryable
from polygate.providers import Provider
from polygate.router import Route, Router
from polygate.stats import Stats


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO_KEY", "secret123")
    p = tmp_path / "c.yaml"
    p.write_text("auth:\n  api_key: ${FOO_KEY}\nmissing: ${NOT_SET}\n", encoding="utf-8")
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
