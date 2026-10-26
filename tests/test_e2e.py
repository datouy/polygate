"""端到端测试：起一个本地 Mock 上游，验证网关路由 + 非流式 + 流式透传。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from polygate.server import create_app


class MockUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        stream = bool(body.get("stream", False))
        self.send_response(200)
        if stream:
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "id": "mock-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello world"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))


def _start_server():
    srv = HTTPServer(("127.0.0.1", 0), MockUpstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _make_app(port: int):
    return create_app(
        {
            "providers": {
                "mock": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "x"}
            },
            "models": [
                {"name": "mock-chat", "providers": [{"provider": "mock", "model": "mock-chat"}]}
            ],
        }
    )


def test_e2e():
    srv = _start_server()
    port = srv.server_address[1]
    app = _make_app(port)
    try:
        with TestClient(app) as c:
            assert c.get("/healthz").status_code == 200

            models = c.get("/v1/models").json()
            assert len(models["data"]) == 1
            assert models["data"][0]["id"] == "mock-chat"

            r = c.post(
                "/v1/chat/completions",
                json={"model": "mock-chat", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "hello world"

            # 未知模型 -> 404
            r = c.post("/v1/chat/completions", json={"model": "nope", "messages": []})
            assert r.status_code == 404

            # 流式透传
            with c.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "mock-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as r:
                body = "".join(r.iter_text())
                assert "[DONE]" in body
                assert "hello" in body

            # 用量统计
            usage = c.get("/v1/usage").json()
            assert usage["models"]["mock-chat"]["calls"] >= 2
    finally:
        srv.shutdown()
