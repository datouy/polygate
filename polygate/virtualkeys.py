"""Persistent virtual API keys.

Only a SHA-256 digest and a short display prefix are stored.  The generated
secret is returned exactly once by the create/rotate operation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .clients import ClientPolicy

_KEY_PREFIX = "pg_"


def new_client_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_client_key(key: str) -> str:
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


def key_preview(key: str) -> str:
    key = str(key or "")
    return f"{key[:11]}…" if key else ""


@dataclass(frozen=True)
class ManagedClientKey:
    id: str
    key_hash: str
    key_prefix: str
    name: str
    models: tuple[str, ...]
    rpm: float
    rpm_burst: float
    daily_calls: int
    monthly_calls: int
    monthly_budget: float
    expires_at: float | None
    note: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None
    rotated_at: float | None

    def policy(self) -> ClientPolicy:
        return ClientPolicy(
            name=self.name,
            models=set(self.models),
            rpm=self.rpm,
            rpm_burst=self.rpm_burst,
            daily_calls=self.daily_calls,
            monthly_calls=self.monthly_calls,
            monthly_budget=self.monthly_budget,
            id=self.id,
            key_prefix=self.key_prefix,
            expires_at=self.expires_at,
        )

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "models": list(self.models),
            "rpm": self.rpm,
            "rpm_burst": self.rpm_burst,
            "daily_calls": self.daily_calls,
            "monthly_calls": self.monthly_calls,
            "monthly_budget": self.monthly_budget,
            "expires_at": self.expires_at,
            "note": self.note,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
            "rotated_at": self.rotated_at,
            "expired": bool(self.expires_at and self.expires_at <= time.time()),
            "active": not self.revoked_at and not (self.expires_at and self.expires_at <= time.time()),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL,
    models_json TEXT NOT NULL DEFAULT '[]',
    rpm REAL NOT NULL DEFAULT 0,
    rpm_burst REAL NOT NULL DEFAULT 0,
    daily_calls INTEGER NOT NULL DEFAULT 0,
    monthly_calls INTEGER NOT NULL DEFAULT 0,
    monthly_budget REAL NOT NULL DEFAULT 0,
    expires_at REAL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL,
    rotated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_client_keys_active ON client_keys(revoked_at, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_keys_name_active
    ON client_keys(name) WHERE revoked_at IS NULL;
"""


class VirtualKeyStore:
    """Small SQLite-backed lifecycle store also usable without a database."""

    def __init__(self, db_path: str | None = None):
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._memory: dict[str, dict[str, Any]] = {}
        self._last_touch: dict[str, float] = {}
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _row(row: tuple[Any, ...]) -> ManagedClientKey:
        try:
            models = json.loads(row[4]) if row[4] else []
        except Exception:
            models = []
        return ManagedClientKey(
            id=str(row[0]),
            key_hash=str(row[1]),
            key_prefix=str(row[2]),
            name=str(row[3]),
            models=tuple(str(v) for v in models if str(v).strip()),
            rpm=float(row[5] or 0),
            rpm_burst=float(row[6] or 0),
            daily_calls=int(row[7] or 0),
            monthly_calls=int(row[8] or 0),
            monthly_budget=float(row[9] or 0),
            expires_at=float(row[10]) if row[10] is not None else None,
            note=str(row[11] or ""),
            created_at=float(row[12]),
            last_used_at=float(row[13]) if row[13] is not None else None,
            revoked_at=float(row[14]) if row[14] is not None else None,
            rotated_at=float(row[15]) if row[15] is not None else None,
        )

    _SELECT = (
        "SELECT id,key_hash,key_prefix,name,models_json,rpm,rpm_burst,daily_calls,"
        "monthly_calls,monthly_budget,expires_at,note,created_at,last_used_at,"
        "revoked_at,rotated_at FROM client_keys"
    )

    def _fetch(self, where: str = "", params: tuple[Any, ...] = ()) -> list[ManagedClientKey]:
        sql = self._SELECT + (" " + where if where else "") + " ORDER BY created_at DESC"
        if self._conn is None:
            rows = sorted(self._memory.values(), key=lambda item: item["created_at"], reverse=True)
            if where == "WHERE id = ?":
                rows = [item for item in rows if item["id"] == params[0]]
            elif where == "WHERE key_hash = ?":
                rows = [item for item in rows if item["key_hash"] == params[0]]
            elif where == "WHERE revoked_at IS NULL":
                rows = [item for item in rows if item["revoked_at"] is None]
            return [self._row(tuple(item[k] for k in (
                "id", "key_hash", "key_prefix", "name", "models_json", "rpm", "rpm_burst",
                "daily_calls", "monthly_calls", "monthly_budget", "expires_at", "note",
                "created_at", "last_used_at", "revoked_at", "rotated_at",
            ))) for item in rows]
        with self._lock:
            return [self._row(row) for row in self._conn.execute(sql, params).fetchall()]

    def list(self, include_revoked: bool = True) -> list[ManagedClientKey]:
        return self._fetch("" if include_revoked else "WHERE revoked_at IS NULL")

    def runtime_records(self) -> list[ManagedClientKey]:
        return self.list(include_revoked=False)

    def get(self, key_id: str) -> ManagedClientKey | None:
        rows = self._fetch("WHERE id = ?", (str(key_id),))
        return rows[0] if rows else None

    def get_by_hash(self, key_hash: str) -> ManagedClientKey | None:
        rows = self._fetch("WHERE key_hash = ?", (str(key_hash),))
        return rows[0] if rows else None

    def _values(
        self,
        *,
        key_hash: str,
        key_prefix: str,
        name: str,
        models: list[str],
        rpm: float,
        rpm_burst: float,
        daily_calls: int,
        monthly_calls: int,
        monthly_budget: float,
        expires_at: float | None,
        note: str,
    ) -> tuple[Any, ...]:
        now = time.time()
        return (
            uuid.uuid4().hex, key_hash, key_prefix, name,
            json.dumps([str(v).strip() for v in models if str(v).strip()], ensure_ascii=False),
            float(rpm), float(rpm_burst), int(daily_calls), int(monthly_calls), float(monthly_budget),
            expires_at, str(note or ""), now, None, None, None,
        )

    def _insert(self, values: tuple[Any, ...]) -> ManagedClientKey:
        names = (
            "id", "key_hash", "key_prefix", "name", "models_json", "rpm", "rpm_burst",
            "daily_calls", "monthly_calls", "monthly_budget", "expires_at", "note",
            "created_at", "last_used_at", "revoked_at", "rotated_at",
        )
        item = dict(zip(names, values, strict=True))
        if self._conn is None:
            with self._lock:
                if any(v["key_hash"] == item["key_hash"] for v in self._memory.values()):
                    raise ValueError("key already exists")
                if any(v["name"] == item["name"] and v["revoked_at"] is None for v in self._memory.values()):
                    raise ValueError(f"client key name already exists: {item['name']}")
                self._memory[item["id"]] = item
        else:
            try:
                with self._lock:
                    self._conn.execute(
                        "INSERT INTO client_keys(" + ",".join(names) + ") VALUES(" + ",".join("?" for _ in names) + ")",
                        values,
                    )
                    self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(str(exc)) from None
        result = self.get(item["id"])
        if result is None:
            raise RuntimeError("client key insert did not persist")
        return result

    def create(
        self,
        *,
        name: str,
        models: list[str] | None = None,
        rpm: float = 0,
        rpm_burst: float = 0,
        daily_calls: int = 0,
        monthly_calls: int = 0,
        monthly_budget: float = 0,
        expires_at: float | None = None,
        note: str = "",
        raw_key: str | None = None,
    ) -> tuple[ManagedClientKey, str]:
        secret = str(raw_key or new_client_key())
        record = self._insert(self._values(
            key_hash=hash_client_key(secret),
            key_prefix=key_preview(secret),
            name=name,
            models=models or [],
            rpm=rpm,
            rpm_burst=rpm_burst,
            daily_calls=daily_calls,
            monthly_calls=monthly_calls,
            monthly_budget=monthly_budget,
            expires_at=expires_at,
            note=note,
        ))
        return record, secret

    def import_legacy(self, items: list[dict] | None) -> int:
        imported = 0
        for index, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            raw = str(item.get("key") or "").strip()
            if not raw or self.get_by_hash(hash_client_key(raw)) is not None:
                continue
            name = str(item.get("name") or f"legacy-{index + 1}").strip()
            if any(row.name == name and row.revoked_at is None for row in self.list(False)):
                name = f"{name}-{index + 1}"
            try:
                self.create(
                    name=name,
                    models=list(item.get("models") or []),
                    rpm=float(item.get("rpm") or 0),
                    rpm_burst=float(item.get("rpm_burst") or item.get("rpm") or 0),
                    daily_calls=int(item.get("daily_calls") or 0),
                    monthly_calls=int(item.get("monthly_calls") or 0),
                    monthly_budget=float(item.get("monthly_budget") or 0),
                    note="Imported from legacy client_keys",
                    raw_key=raw,
                )
            except (TypeError, ValueError):
                continue
            imported += 1
        return imported

    def update(self, key_id: str, patch: dict[str, Any]) -> ManagedClientKey:
        current = self.get(key_id)
        if current is None:
            raise KeyError(key_id)
        fields: dict[str, Any] = {}
        for key in (
            "name", "models", "rpm", "rpm_burst", "daily_calls", "monthly_calls",
            "monthly_budget", "expires_at", "note",
        ):
            if key in patch:
                fields[key] = patch[key]
        if "models" in fields:
            fields["models_json"] = json.dumps(
                [str(v).strip() for v in (fields.pop("models") or []) if str(v).strip()],
                ensure_ascii=False,
            )
        if not fields:
            return current
        if "name" in fields:
            fields["name"] = str(fields["name"] or "").strip()
            if not fields["name"]:
                raise ValueError("name is required")
        for key in ("rpm", "rpm_burst", "monthly_budget"):
            if key in fields:
                fields[key] = float(fields[key] or 0)
        for key in ("daily_calls", "monthly_calls"):
            if key in fields:
                fields[key] = int(fields[key] or 0)
        if "expires_at" in fields and fields["expires_at"] is not None:
            fields["expires_at"] = float(fields["expires_at"])
        if "note" in fields:
            fields["note"] = str(fields["note"] or "")

        assignments = ",".join(f"{name}=?" for name in fields)
        params = tuple(fields.values()) + (str(key_id),)
        if self._conn is None:
            with self._lock:
                item = self._memory.get(str(key_id))
                if item is None:
                    raise KeyError(key_id)
                candidate = {**item, **fields}
                if any(
                    v["id"] != candidate["id"] and v["name"] == candidate["name"] and v["revoked_at"] is None
                    for v in self._memory.values()
                ):
                    raise ValueError(f"client key name already exists: {candidate['name']}")
                self._memory[str(key_id)] = candidate
        else:
            try:
                with self._lock:
                    self._conn.execute(f"UPDATE client_keys SET {assignments} WHERE id=?", params)
                    self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(str(exc)) from None
        result = self.get(key_id)
        if result is None:
            raise KeyError(key_id)
        return result

    def rotate(self, key_id: str) -> tuple[ManagedClientKey, str]:
        current = self.get(key_id)
        if current is None:
            raise KeyError(key_id)
        if current.revoked_at:
            raise ValueError("client key is revoked")
        secret = new_client_key()
        now = time.time()
        fields = {
            "key_hash": hash_client_key(secret),
            "key_prefix": key_preview(secret),
            "rotated_at": now,
        }
        assignments = ",".join(f"{name}=?" for name in fields)
        params = tuple(fields.values()) + (str(key_id),)
        if self._conn is None:
            with self._lock:
                self._memory[str(key_id)] = {**self._memory[str(key_id)], **fields}
        else:
            with self._lock:
                self._conn.execute(f"UPDATE client_keys SET {assignments} WHERE id=?", params)
                self._conn.commit()
        result = self.get(key_id)
        if result is None:
            raise KeyError(key_id)
        return result, secret

    def revoke(self, key_id: str) -> ManagedClientKey:
        current = self.get(key_id)
        if current is None:
            raise KeyError(key_id)
        if current.revoked_at:
            return current
        now = time.time()
        if self._conn is None:
            with self._lock:
                self._memory[str(key_id)]["revoked_at"] = now
        else:
            with self._lock:
                self._conn.execute("UPDATE client_keys SET revoked_at=? WHERE id=?", (now, str(key_id)))
                self._conn.commit()
        result = self.get(key_id)
        if result is None:
            raise KeyError(key_id)
        return result

    def touch(self, key_id: str) -> None:
        if not key_id:
            return
        now = time.time()
        with self._lock:
            if now - self._last_touch.get(key_id, 0) < 60:
                return
            self._last_touch[key_id] = now
            try:
                if self._conn is None:
                    item = self._memory.get(key_id)
                    if item is not None:
                        item["last_used_at"] = now
                else:
                    self._conn.execute("UPDATE client_keys SET last_used_at=? WHERE id=?", (now, key_id))
                    self._conn.commit()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
