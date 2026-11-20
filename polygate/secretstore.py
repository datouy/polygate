"""Encrypted provider credentials for persisted configuration overrides.

Provider keys are encrypted before they reach SQLite. Configuration overrides
hold only a versioned reference, so replacing a key never mutates the active
snapshot until the configuration transaction succeeds.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

REF_PREFIX = "secret://"
_CHECK = b"polygate-provider-secrets-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_secrets (
    ref TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_secret_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    check_value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SecretStoreError(RuntimeError):
    """Raised when encrypted provider credentials cannot be opened."""


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(b"polygate-provider-secrets-v1\0" + secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _default_key_path(db_path: str) -> Path | None:
    configured = os.environ.get("POLYGATE_MASTER_KEY_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    path = Path(db_path)
    if str(path) == ":memory:":
        return None
    return path.with_name(f"{path.name}.master-key")


def _read_or_create_local_key(db_path: str) -> str:
    path = _default_key_path(db_path)
    if path is None:
        return secrets.token_urlsafe(48)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise SecretStoreError(f"master key file is empty: {path}")
        return value

    value = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
    return value


def _master_secret(db_path: str | None) -> str:
    configured = os.environ.get("POLYGATE_MASTER_KEY", "").strip()
    if configured:
        return configured
    if db_path:
        return _read_or_create_local_key(db_path)
    # A process-local database also gets a process-local encryption key.
    return secrets.token_urlsafe(48)


class ProviderSecretStore:
    """SQLite-backed encrypted provider keys with transactional-style staging."""

    def __init__(self, db_path: str | None = None):
        self._lock = threading.Lock()
        self._fernet = _fernet(_master_secret(db_path))
        self._conn: sqlite3.Connection | None = None
        self._memory: dict[str, tuple[str, float]] = {}
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._verify_master_key()

    def _encrypt(self, value: bytes) -> str:
        return self._fernet.encrypt(value).decode("ascii")

    def _decrypt(self, value: str) -> bytes:
        try:
            return self._fernet.decrypt(value.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise SecretStoreError(
                "cannot decrypt provider credentials; POLYGATE_MASTER_KEY or its key file changed"
            ) from exc

    def _verify_master_key(self) -> None:
        with self._lock:
            row = (
                self._conn.execute("SELECT check_value FROM provider_secret_meta WHERE id=1").fetchone()
                if self._conn is not None
                else None
            )
            if row is None:
                value = self._encrypt(_CHECK)
                if self._conn is not None:
                    self._conn.execute(
                        "INSERT INTO provider_secret_meta(id, check_value, updated_at) VALUES(1, ?, ?)",
                        (value, time.time()),
                    )
                    self._conn.commit()
                return
            if self._decrypt(str(row[0])) != _CHECK:
                raise SecretStoreError("provider credential master key check failed")

    def put(self, payload: dict[str, Any]) -> str:
        """Encrypt one immutable provider secret and return its reference."""
        clean: dict[str, Any] = {}
        if "api_key" in payload:
            value = str(payload.get("api_key") or "").strip()
            clean["api_key"] = value or None
        if "api_keys" in payload:
            values = payload.get("api_keys") or []
            clean["api_keys"] = [str(value).strip() for value in values if str(value).strip()]
        if not any(clean.get(key) for key in ("api_key", "api_keys")):
            raise ValueError("provider secret cannot be empty")
        ref = REF_PREFIX + uuid.uuid4().hex
        encoded = self._encrypt(json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        now = time.time()
        with self._lock:
            if self._conn is None:
                self._memory[ref] = (encoded, now)
            else:
                self._conn.execute(
                    "INSERT INTO provider_secrets(ref, ciphertext, created_at) VALUES(?, ?, ?)",
                    (ref, encoded, now),
                )
                self._conn.commit()
        return ref

    def get(self, ref: str) -> dict[str, Any]:
        with self._lock:
            if self._conn is None:
                row = self._memory.get(str(ref))
            else:
                row = self._conn.execute(
                    "SELECT ciphertext, created_at FROM provider_secrets WHERE ref=?", (str(ref),)
                ).fetchone()
        if row is None:
            raise SecretStoreError(f"provider secret reference not found: {ref}")
        try:
            payload = json.loads(self._decrypt(str(row[0])).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStoreError("provider secret has an invalid payload") from exc
        return payload if isinstance(payload, dict) else {}

    def stage_overrides(self, overrides: dict) -> tuple[dict, set[str]]:
        """Replace plaintext provider keys and return newly created references."""
        result = copy.deepcopy(overrides or {})
        created: set[str] = set()
        providers = result.get("providers")
        if not isinstance(providers, dict):
            return result, created
        for patch in providers.values():
            if not isinstance(patch, dict):
                continue
            has_plaintext = "api_key" in patch or "api_keys" in patch
            if not has_plaintext:
                continue
            old_ref = str(patch.get("api_key_ref") or "")
            payload: dict[str, Any] = {}
            if old_ref.startswith(REF_PREFIX):
                payload.update(self.get(old_ref))
            for field in ("api_key", "api_keys"):
                if field in patch:
                    payload[field] = patch.get(field)
            patch.pop("api_key", None)
            patch.pop("api_keys", None)
            if any(payload.get(key) for key in ("api_key", "api_keys")):
                ref = self.put(payload)
                patch["api_key_ref"] = ref
                created.add(ref)
            else:
                patch.pop("api_key_ref", None)
        return result, created

    def resolve_config(self, cfg: dict) -> dict:
        """Return a runtime copy with encrypted references replaced by secrets."""
        result = copy.deepcopy(cfg or {})
        providers = result.get("providers")
        if not isinstance(providers, dict):
            return result
        for name, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            ref = str(provider.pop("api_key_ref", "") or "")
            if not ref:
                continue
            if not ref.startswith(REF_PREFIX):
                raise SecretStoreError(f"invalid provider secret reference for {name}")
            secret = self.get(ref)
            for field in ("api_key", "api_keys"):
                if field in secret:
                    provider[field] = copy.deepcopy(secret[field])
        return result

    def resolve_merged_config(self, cfg: dict, overrides: dict) -> dict:
        """Resolve refs while letting unsaved plaintext patch values win."""
        result = copy.deepcopy(cfg or {})
        providers = result.get("providers")
        patches = (overrides or {}).get("providers")
        if isinstance(providers, dict) and isinstance(patches, dict):
            for name, patch in patches.items():
                if not isinstance(patch, dict) or not ({"api_key", "api_keys"} & patch.keys()):
                    continue
                provider = providers.get(name)
                if not isinstance(provider, dict):
                    continue
                payload: dict[str, Any] = {}
                old_ref = str(patch.get("api_key_ref") or "")
                if old_ref.startswith(REF_PREFIX):
                    payload.update(self.get(old_ref))
                for field in ("api_key", "api_keys"):
                    if field in patch:
                        payload[field] = patch.get(field)
                for field in ("api_key", "api_keys"):
                    if field in payload:
                        provider[field] = copy.deepcopy(payload[field])
        return self.resolve_config(result)

    def referenced(self, overrides: dict) -> set[str]:
        refs: set[str] = set()
        providers = (overrides or {}).get("providers")
        if isinstance(providers, dict):
            for patch in providers.values():
                if isinstance(patch, dict):
                    ref = str(patch.get("api_key_ref") or "")
                    if ref.startswith(REF_PREFIX):
                        refs.add(ref)
        return refs

    def prune(self, keep: set[str] | None = None) -> int:
        """Remove unreferenced staged keys, including crash-left orphans."""
        keep = keep or set()
        with self._lock:
            if self._conn is None:
                stale = set(self._memory) - keep
                for ref in stale:
                    self._memory.pop(ref, None)
                return len(stale)
            rows = self._conn.execute("SELECT ref FROM provider_secrets").fetchall()
            stale = [str(row[0]) for row in rows if row[0] not in keep]
            if stale:
                self._conn.executemany("DELETE FROM provider_secrets WHERE ref=?", ((ref,) for ref in stale))
                self._conn.commit()
            return len(stale)

    def delete(self, refs: set[str]) -> None:
        if not refs:
            return
        with self._lock:
            if self._conn is None:
                for ref in refs:
                    self._memory.pop(ref, None)
            else:
                self._conn.executemany("DELETE FROM provider_secrets WHERE ref=?", ((ref,) for ref in refs))
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
