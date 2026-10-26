"""管理面板配置接口测试：GET 脱敏 / PUT 热重载 / 存 SQLite（yaml 不动）/ 重置。"""

import yaml
from fastapi.testclient import TestClient

from polygate.server import create_app


def _cfg():
    """每次返回全新的内存配置（db_path=None → 覆盖存进程内存）。"""
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
        assert "old-secret-key-1234" not in r.text  # 绝不返回明文


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

        # 空 key 不覆盖已有关键字
        c.put("/v1/config", json={"providers": {"mock": {"base_url": "http://127.0.0.1:7/v1"}}})
        d = c.get("/v1/config").json()
        assert d["providers"]["mock"]["base_url"] == "http://127.0.0.1:7/v1"
        assert d["providers"]["mock"]["key_hint"] == "new-...5678"


def test_config_persists_in_sqlite_and_yaml_untouched(tmp_path):
    yaml_path = tmp_path / "cfg.yaml"
    db = tmp_path / "x.db"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "db_path": str(db),
                "health": {"enabled": False},
                "providers": {"mock": {"base_url": "http://a/v1", "api_key": "k-1111-aaaa"}},
                "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
            }
        ),
        encoding="utf-8",
    )
    original_yaml = yaml_path.read_text(encoding="utf-8")

    app = create_app(str(yaml_path))
    with TestClient(app) as c:
        r = c.put("/v1/config", json={"providers": {"mock": {"base_url": "http://b/v1", "api_key": "k-2222-bbbb"}}})
        assert r.status_code == 200
        assert c.get("/v1/config").json()["providers"]["mock"]["base_url"] == "http://b/v1"

    # 1) config.yaml 未被改动
    assert yaml_path.read_text(encoding="utf-8") == original_yaml

    # 2) 新 app（模拟重启）从同一 yaml + db 启动，覆盖仍生效
    app2 = create_app(str(yaml_path))
    with TestClient(app2) as c2:
        d = c2.get("/v1/config").json()
        assert d["providers"]["mock"]["base_url"] == "http://b/v1"
        assert d["providers"]["mock"]["key_hint"] == "k-22...bbbb"


def test_config_delete_resets_to_yaml():
    app = create_app(_cfg())
    with TestClient(app) as c:
        c.put("/v1/config", json={"providers": {"mock": {"base_url": "http://zzz/v1"}}})
        assert c.get("/v1/config").json()["providers"]["mock"]["base_url"] == "http://zzz/v1"
        # 清除覆盖 → 回到 yaml 原值
        r = c.delete("/v1/config")
        assert r.status_code == 200
        assert c.get("/v1/config").json()["providers"]["mock"]["base_url"] == "http://127.0.0.1:9/v1"


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
