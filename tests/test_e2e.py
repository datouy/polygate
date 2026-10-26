"""端到端测试：起本地 Mock 上游，验证路由 + 非流式 + 流式 + embeddings + 面板。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from polygate.server import create_app


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Connection", "close")

        if self.path.endswith("/embeddings"):
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": body.get("model", "emb"),
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            self.wfile.flush()
            return

        stream = bool(body.get("stream", False))
        if stream:
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
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
            self.wfile.flush()


def _start_server():
    srv = HTTPServer(("127.0.0.1", 0), MockUpstream)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _make_app(port: int, db_path: str):
    return create_app(
        {
            "db_path": db_path,
            "health": {"enabled": False},
            "providers": {
                "mock": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "x"}
            },
            "models": [
                {"name": "mock-chat", "providers": [{"provider": "mock", "model": "mock-chat"}]},
                {
                    "name": "mock-emb",
                    "kind": "embeddings",
                    "providers": [{"provider": "mock", "model": "mock-emb"}],
                },
            ],
        }
    )


def test_e2e(tmp_path):
    srv = _start_server()
    port = srv.server_address[1]
    app = _make_app(port, str(tmp_path / "t.db"))
    try:
        with TestClient(app) as c:
            assert c.get("/healthz").status_code == 200

            models = c.get("/v1/models").json()
            assert {m["id"] for m in models["data"]} == {"mock-chat", "mock-emb"}

            # 非流式对话
            r = c.post(
                "/v1/chat/completions",
                json={"model": "mock-chat", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "hello world"

            # 未知模型
            assert c.post("/v1/chat/completions", json={"model": "nope", "messages": []}).status_code == 404

            # 流式透传
            with c.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "mock-chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as r:
                body = "".join(r.iter_text())
                assert "[DONE]" in body and "hello" in body

            # Embeddings 统一转发
            r = c.post("/v1/embeddings", json={"model": "mock-emb", "input": "hello"})
            assert r.status_code == 200
            assert r.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]

            # 状态（熔断/限流）
            st = c.get("/v1/status").json()
            assert st["providers"]["mock"]["state"] == "closed"
            assert st["models"][0]["kind"] == "chat"

            # 用量统计（含持久化）
            u = c.get("/v1/usage").json()
            assert u["models"]["mock-chat"]["calls"] >= 2
            assert u["persisted_total_calls"] >= 3

            # 每日历史
            h = c.get("/v1/usage/history?days=7").json()
            assert len(h) >= 1 and h[0]["calls"] >= 3

            # 管理面板
            r = c.get("/admin")
            assert r.status_code == 200 and "polygate" in r.text
    finally:
        srv.shutdown()
