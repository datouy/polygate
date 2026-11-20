import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from polygate.configstore import ConfigStore
from polygate.secretstore import SecretStoreError
from polygate.server import create_app


def _cfg(db_path):
    return {
        "db_path": str(db_path),
        "health": {"enabled": False},
        "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "base-key-1234"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    }


def _database_text(db_path):
    with sqlite3.connect(db_path) as conn:
        override = conn.execute("SELECT data FROM config_overrides WHERE id=1").fetchone()
        secrets = conn.execute("SELECT ref, ciphertext FROM provider_secrets").fetchall()
    return str(override or "") + str(secrets)


def test_legacy_provider_key_is_encrypted_and_migrated_on_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYGATE_MASTER_KEY", raising=False)
    monkeypatch.delenv("POLYGATE_MASTER_KEY_FILE", raising=False)
    db = tmp_path / "gateway.db"
    cfg = _cfg(db)
    store = ConfigStore(str(db))
    store.save(
        {
            "providers": {
                "mock": {"base_url": "http://127.0.0.1:8/v1", "api_key": "legacy-provider-key-9876"}
            }
        }
    )
    store.close()

    app = create_app(cfg)
    with TestClient(app) as client:
        view = client.get("/v1/config").json()["providers"]["mock"]
        assert view["key_hint"] == "lega...9876"

    persisted = _database_text(db)
    assert "legacy-provider-key-9876" not in persisted
    assert "api_key_ref" in persisted
    assert (tmp_path / "gateway.db.master-key").exists()

    restarted = create_app(cfg)
    with TestClient(restarted) as client:
        view = client.get("/v1/config").json()["providers"]["mock"]
        assert view["has_key"] is True
        assert view["key_hint"] == "lega...9876"


def test_provider_secret_requires_the_same_master_key(tmp_path, monkeypatch):
    master = "unit-test-master-key-with-enough-entropy"
    db = tmp_path / "secure.db"
    monkeypatch.setenv("POLYGATE_MASTER_KEY", master)
    app = create_app(_cfg(db))
    with TestClient(app) as client:
        saved = client.put(
            "/v1/config",
            json={"providers": {"mock": {"base_url": "http://127.0.0.1:8/v1", "api_key": "new-secret-abcd"}}},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["diff"][0]["detail"] == "Base URL、密钥"

    with sqlite3.connect(db) as conn:
        override = conn.execute("SELECT data FROM config_overrides WHERE id=1").fetchone()[0]
        payload = json.loads(override)
        ciphertext = conn.execute("SELECT ciphertext FROM provider_secrets").fetchone()[0]
    assert "api_key" not in payload["providers"]["mock"]
    assert payload["providers"]["mock"]["api_key_ref"].startswith("secret://")
    assert "new-secret-abcd" not in ciphertext

    monkeypatch.setenv("POLYGATE_MASTER_KEY", "wrong-master-key")
    with pytest.raises(SecretStoreError, match="cannot decrypt") as exc:
        create_app(_cfg(db))
    assert "new-secret-abcd" not in str(exc.value)


def test_validate_with_new_key_has_no_persistence_side_effect(tmp_path, monkeypatch):
    master = "validation-master-key-with-enough-entropy"
    db = tmp_path / "validate.db"
    monkeypatch.setenv("POLYGATE_MASTER_KEY", master)
    app = create_app(_cfg(db))
    with TestClient(app) as client:
        result = client.post(
            "/v1/config/validate",
            json={"providers": {"mock": {"base_url": "http://127.0.0.1:8/v1", "api_key": "preview-secret-5678"}}},
        )
        assert result.status_code == 200, result.text
        assert result.json()["diff"]

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_secrets").fetchone()[0] == 0
