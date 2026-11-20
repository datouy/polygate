import sqlite3
import time

from fastapi.testclient import TestClient

from polygate.server import create_app
from polygate.virtualkeys import hash_client_key

ADMIN = {"Authorization": "Bearer admin-secret"}


def _app(db_path):
    return create_app({
        "db_path": str(db_path),
        "health": {"enabled": False},
        "auth": {"api_key": "admin-secret", "admin_key": "admin-secret"},
        "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "upstream"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    })


def test_virtual_key_lifecycle_and_hashed_storage(tmp_path):
    db = tmp_path / "keys.db"
    with TestClient(_app(db)) as client:
        response = client.post(
            "/v1/client-keys",
            headers=ADMIN,
            json={
                "name": "billing",
                "models": ["m"],
                "rpm": 60,
                "monthly_budget": 12,
                "note": "billing service",
            },
        )
        assert response.status_code == 200, response.text
        created = response.json()
        secret = created["key"]
        key_id = created["client_key"]["id"]
        assert secret.startswith("pg_")
        assert secret not in response.text.replace(created["key"], "")

        connection = sqlite3.connect(db)
        row = connection.execute(
            "SELECT key_hash,key_prefix,models_json,monthly_budget FROM client_keys WHERE id=?",
            (key_id,),
        ).fetchone()
        raw_rows = connection.execute("SELECT * FROM client_keys").fetchall()
        connection.close()
        assert row[0] == hash_client_key(secret)
        assert row[1].endswith("…") and secret not in str(raw_rows)
        assert row[2] == '["m"]'
        assert row[3] == 12

        listed = client.get("/v1/client-keys", headers=ADMIN)
        assert listed.status_code == 200
        assert secret not in listed.text
        assert row[0] not in listed.text

        used = client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        assert used.status_code == 200
        after_use = client.get("/v1/client-keys", headers=ADMIN).json()[0]
        assert after_use["last_used_at"] is not None

        rotated = client.post(f"/v1/client-keys/{key_id}/rotate", headers=ADMIN)
        assert rotated.status_code == 200, rotated.text
        replacement = rotated.json()["key"]
        assert replacement != secret
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"}).status_code == 401
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {replacement}"}).status_code == 200

        revoked = client.delete(f"/v1/client-keys/{key_id}", headers=ADMIN)
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {replacement}"}).status_code == 401


def test_virtual_key_expiry_and_legacy_migration(tmp_path):
    db = tmp_path / "migration.db"
    app = create_app({
        "db_path": str(db),
        "health": {"enabled": False},
        "auth": {
            "api_key": "admin-secret",
            "admin_key": "admin-secret",
            "client_keys": [{"key": "legacy-secret", "name": "legacy", "models": ["m"]}],
        },
        "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "upstream"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    })
    with TestClient(app) as client:
        assert client.get(
            "/v1/models", headers={"Authorization": "Bearer legacy-secret"}
        ).status_code == 200
        records = client.get("/v1/client-keys", headers=ADMIN).json()
        migrated = next(record for record in records if record["name"] == "legacy")
        assert migrated["key_prefix"] != "legacy-secret"

        response = client.post(
            "/v1/client-keys",
            headers=ADMIN,
            json={"name": "short-lived", "expires_at": time.time() + 60},
        )
        assert response.status_code == 200
        secret = response.json()["key"]
        key_id = response.json()["client_key"]["id"]
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
        patched = client.patch(
            f"/v1/client-keys/{key_id}",
            headers=ADMIN,
            json={"expires_at": time.time() - 1},
        )
        assert patched.status_code == 200, patched.text
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"}).status_code == 401

    connection = sqlite3.connect(db)
    raw = str(connection.execute("SELECT * FROM client_keys").fetchall())
    connection.close()
    assert "legacy-secret" not in raw
