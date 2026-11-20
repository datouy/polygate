import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from polygate.server import create_app


class ModelsHandler(BaseHTTPRequestHandler):
    seen_auth = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen_auth = self.headers.get("Authorization", "")
        if self.path != "/v1/models":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != "Bearer transient-secret":
            payload = b'{"error":"unauthorized"}'
            self.send_response(401)
        else:
            payload = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_provider_connectivity_uses_unsaved_values_without_echoing_key(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    port = upstream.server_address[1]
    app = create_app({
        "db_path": str(tmp_path / "provider.db"),
        "health": {"enabled": False},
        "auth": {"api_key": "admin", "admin_key": "admin"},
        "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "saved-secret"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    })
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/providers/mock/test",
                headers={"Authorization": "Bearer admin"},
                json={
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api_key": "transient-secret",
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["ok"] is True
            assert result["models"] == ["model-a", "model-b"]
            assert result["endpoint_present"] is True
            assert "transient-secret" not in response.text
            assert ModelsHandler.seen_auth == "Bearer transient-secret"

            unauthorized = client.post(
                "/v1/providers/mock/test",
                headers={"Authorization": "Bearer admin"},
                json={"base_url": f"http://127.0.0.1:{port}/v1"},
            )
            assert unauthorized.status_code == 200
            assert unauthorized.json()["ok"] is False
            assert unauthorized.json()["reachable"] is True
            assert unauthorized.json()["status_code"] == 401
            assert "saved-secret" not in unauthorized.text
    finally:
        upstream.shutdown()
        thread.join(timeout=2)
