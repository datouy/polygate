"""管理面板配置接口测试（GET 脱敏 / PUT 热重载 / 持久化 / 新增模型）。"""

import yaml
from fastapi.testclient import TestClient

from polygate.server import create_app


def _cfg():
    """每次返回全新的配置字典（避免测试间共享嵌套对象）。"""
    return {
        "db_path": None,
        "health": {"enabled": False},
        "providers": {
            "mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "old-secret-key-1234"},
        },
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    }


def test_config_get_masks_key():
    app = create_app(_cfg())
    with TestClient(app) as c:
        r = c.get("/v1/config")
        assert r.status_code == 200
        prov = r.json()["providers"]["mock"]
        assert prov["base_url"] == "http://127.0.0.1:9/v1"
        assert prov["has_key"] is True
        assert prov["key_hint"] == "old-...1234"
        # 绝不返回明文
        assert "old-secret-key-1234" not in r.text


def test_config_put_updates_and_reloads():
    app = create_app(_cfg())
    with TestClient(app) as c:
        r = c.put(
            "/v1/config",
            json={"providers": {"mock": {"base_url": "http://127.0.0.1:8/v1", "api_key": "new-key-5678"}}},
        )
        assert r.status_code == 200 and r.json()["ok"] is True

        d = c.get("/v1/config").json()
        assert d["providers"]["mock"]["base_url"] == "http://127.0.0.1:8/v1"
        assert d["providers"]["mock"]["key_hint"] == "new-...5678"

        # 空 key 不覆盖已有 key
        c.put("/v1/config", json={"providers": {"mock": {"base_url": "http://127.0.0.1:7/v1"}}})
        d = c.get("/v1/config").json()
        assert d["providers"]["mock"]["base_url"] == "http://127.0.0.1:7/v1"
        assert d["providers"]["mock"]["key_hint"] == "new-...5678"


def test_config_put_persists_to_file(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "db_path": None,
                "health": {"enabled": False},
                "providers": {"mock": {"base_url": "http://a/v1", "api_key": "k-1111"}},
                "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
            }
        ),
        encoding="utf-8",
    )
    app = create_app(str(path))
    with TestClient(app) as c:
        c.put("/v1/config", json={"providers": {"mock": {"base_url": "http://b/v1", "api_key": "k-2222"}}})

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["providers"]["mock"]["base_url"] == "http://b/v1"
    assert saved["providers"]["mock"]["api_key"] == "k-2222"


def test_config_put_adds_model():
    app = create_app(_cfg())
    with TestClient(app) as c:
        r = c.put(
            "/v1/config",
            json={
                "providers": {},
                "models": [{"name": "newm", "kind": "chat", "providers": [{"provider": "mock", "model": "newm"}]}],
            },
        )
        assert r.status_code == 200
        ids = {m["id"] for m in c.get("/v1/models").json()["data"]}
        assert "newm" in ids and "m" in ids
