"""请求体大小限制：覆盖普通请求与语音转写路径。"""

from fastapi.testclient import TestClient

import polygate.server as server_mod


def _app():
    return server_mod.create_app(
        {
            "db_path": None,
            "health": {"enabled": False},
            "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "x"}},
            "models": [
                {"name": "m", "providers": [{"provider": "mock", "model": "m"}]},
                {"name": "asr", "kind": "audio", "providers": [{"provider": "mock", "model": "asr"}]},
            ],
        }
    )


def test_chat_body_too_large(monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_REQUEST_BYTES", 256)
    app = _app()
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "x" * 1000}]},
        )
        assert r.status_code == 413
        assert "request_too_large" in r.text


def test_audio_body_too_large(monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_AUDIO_BYTES", 128)
    app = _app()
    with TestClient(app) as c:
        r = c.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.mp3", b"x" * 1024, "audio/mpeg")},
            data={"model": "asr"},
        )
        assert r.status_code == 413
        assert "request_too_large" in r.text
