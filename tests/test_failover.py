"""故障切换 / 多 Key 轮询 / 别名重放 的端到端测试。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from polygate.server import create_app


class KeyAwareUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status: int, payload: bytes):
        self.send_response(status)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        body = json.loads(raw or b"{}")
        auth = self.headers.get("Authorization", "")

        if "bad-key" in auth:
            payload = json.dumps({"error": {"message": "invalid api key", "type": "invalid_request_error"}}).encode()
            self._reply(401, payload)
            return

        if self.path.endswith("/embeddings"):
            payload = json.dumps({"object": "list", "data": [{"index": 0, "embedding": [1.0]}], "model": body.get("model")}).encode()
        else:
            payload = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "model": body.get("model"),
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
        self._reply(200, payload)


def _start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), KeyAwareUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_multi_key_401_rotates(tmp_path):
    """第一个 Key 401 时应切换到第二个 Key，而不是直接 502。"""
    srv = _start()
    port = srv.server_address[1]
    app = create_app(
        {
            "db_path": str(tmp_path / "d.db"),
            "health": {"enabled": False},
            "providers": {"mock": {"base_url": f"http://127.0.0.1:{port}/v1", "api_keys": ["bad-key", "good-key"]}},
            "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
        }
    )
    try:
        with TestClient(app) as c:
            r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "ok"
    finally:
        srv.shutdown()


def test_alias_replay_uses_public_model(tmp_path):
    """日志应存对外名 + 上游名；重放用对外名重新路由，而不是拿上游名去查路由表。"""
    srv = _start()
    port = srv.server_address[1]
    app = create_app(
        {
            "db_path": str(tmp_path / "d.db"),
            "health": {"enabled": False},
            "providers": {"mock": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "good-key"}},
            "models": [{"name": "pub-alias", "providers": [{"provider": "mock", "model": "upstream-real"}]}],
        }
    )
    try:
        with TestClient(app) as c:
            r = c.post("/v1/chat/completions", json={"model": "pub-alias", "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 200

            reqs = c.get("/v1/requests").json()
            assert reqs, "应有一条请求日志"
            entry = c.get(f"/v1/requests/{reqs[0]['req_id']}").json()
            assert entry["model"] == "pub-alias"
            assert entry["upstream_model"] == "upstream-real"
            assert entry["request"]["model"] == "pub-alias"

            rp = c.post(f"/v1/requests/{reqs[0]['req_id']}/replay")
            assert rp.status_code == 200
            assert rp.json()["choices"][0]["message"]["content"] == "ok"
    finally:
        srv.shutdown()
