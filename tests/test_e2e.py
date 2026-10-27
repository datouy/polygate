"""端到端测试：起本地 Mock 上游，验证路由、流式、embeddings、语音转写、指标、成本、日志与重放。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from polygate.server import create_app


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        self.send_response(200)
        self.send_header("Connection", "close")
        self.close_connection = True

        if self.path.endswith("/audio/transcriptions"):
            self._send_json({"text": "transcribed text"})
            return

        body = json.loads(raw or b"{}")

        if self.path.endswith("/embeddings"):
            self._send_json(
                {
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                    "model": body.get("model", "emb"),
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                }
            )
            return

        stream = bool(body.get("stream", False))
        if stream:
            sse = (
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)
            self.wfile.flush()
            return

        self._send_json(
            {
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
        )


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
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
                {"name": "mock-emb", "kind": "embeddings", "providers": [{"provider": "mock", "model": "mock-emb"}]},
                {"name": "mock-asr", "kind": "audio", "providers": [{"provider": "mock", "model": "mock-asr"}]},
            ],
            "pricing": {"mock-chat": {"input": 2, "output": 8}},
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
            assert {m["id"] for m in models["data"]} == {"mock-chat"}

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

            # 语音转写转发
            r = c.post(
                "/v1/audio/transcriptions",
                files={"file": ("a.mp3", b"fake-audio", "audio/mpeg")},
                data={"model": "mock-asr"},
            )
            assert r.status_code == 200 and r.json()["text"] == "transcribed text"

            # Prometheus 指标
            r = c.get("/metrics")
            assert r.status_code == 200
            assert "polygate_up 1" in r.text
            assert "polygate_requests_total" in r.text

            # 成本记账
            costs = c.get("/v1/costs").json()
            assert costs["total"] > 0

            # 状态（熔断/限流）
            st = c.get("/v1/status").json()
            assert st["providers"]["mock"]["state"] == "closed"

            # 用量统计（含持久化）
            u = c.get("/v1/usage").json()
            assert u["models"]["mock-chat"]["calls"] >= 2
            # 流式 usage 已在流结束后解析记账（非流式 completion=2，流式 completion=3 → >=3 证明流式已记）
            assert u["models"]["mock-chat"]["completion_tokens"] >= 3
            assert u["persisted_total_calls"] >= 4

            # 请求日志与重放
            reqs = c.get("/v1/requests").json()
            assert len(reqs) >= 4

            # 重放一个非流式 chat 请求
            chat_req = None
            for item in reqs:
                if item["endpoint"] == "/v1/chat/completions" and item["status"] == "ok":
                    d = c.get(f"/v1/requests/{item['req_id']}").json()
                    if not (d.get("request") or {}).get("stream"):
                        chat_req = item
                        break
            assert chat_req is not None
            r = c.post(f"/v1/requests/{chat_req['req_id']}/replay")
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "hello world"

            # 重放 embeddings 请求
            emb_req = next(item for item in reqs if item["endpoint"] == "/v1/embeddings")
            r = c.post(f"/v1/requests/{emb_req['req_id']}/replay")
            assert r.status_code == 200
            assert r.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]

            # 管理面板
            r = c.get("/admin")
            assert r.status_code == 200 and "polygate" in r.text
    finally:
        srv.shutdown()
