"""请求日志与重放单元测试。"""

from polygate.requestlog import RequestLog, _encode_response


def test_request_log(tmp_path):
    rl = RequestLog(str(tmp_path / "r.db"))
    rl.log(
        req_id="abc",
        endpoint="/v1/chat/completions",
        model="m",
        provider="p",
        key_label="k1...abcd",
        status="ok",
        status_code=200,
        latency_ms=100,
        request={"model": "m"},
        response={"choices": []},
        upstream_model="upstream-m",
    )
    rl.log(
        req_id="def",
        endpoint="/v1/chat/completions",
        model="m",
        provider="p",
        key_label="k1...abcd",
        status="error",
        status_code=500,
        latency_ms=50,
        request={"model": "m"},
        error="boom",
    )

    entries = rl.list()
    assert len(entries) == 2
    assert entries[0]["req_id"] == "def"  # 最新在前

    e = rl.get("abc")
    assert e["status"] == "ok"
    assert e["request"] == {"model": "m"}
    assert e["response"] == {"choices": []}
    assert e["upstream_model"] == "upstream-m"
    assert e["truncated"] is False

    assert rl.get("nope") is None
    rl.close()


def test_encode_response_never_produces_broken_json():
    # 超长响应不再字符切片：结构裁剪或整体丢弃，返回的一定是合法 JSON 或 None
    big = {"choices": [{"message": {"content": "x" * 100_000}}]}
    text, truncated = _encode_response(big)
    assert truncated is True
    if text is not None:
        import json

        obj = json.loads(text)
        assert len(text) <= 30_000
        assert obj["choices"][0]["message"]["content"].endswith("…[truncated]")


def test_log_and_get_oversized_response(tmp_path):
    rl = RequestLog(str(tmp_path / "r2.db"))
    big = {"choices": [{"message": {"content": "y" * 100_000}}]}
    rl.log(
        req_id="big",
        endpoint="/v1/chat/completions",
        model="m",
        provider="p",
        key_label="k",
        status="ok",
        response=big,
    )
    e = rl.get("big")
    assert e["truncated"] is True
    # 无论裁剪还是丢弃，get() 都必须能成功解析（不再 500）
    assert e["response"] is None or isinstance(e["response"], dict)
    rl.close()
