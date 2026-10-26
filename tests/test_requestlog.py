"""请求日志与重放单元测试。"""

from polygate.requestlog import RequestLog


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

    assert rl.get("nope") is None
    rl.close()
