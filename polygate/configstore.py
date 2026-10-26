"""配置覆盖存储（SQLite）。

管理面板里修改的配置存这里，**绝不写回 config.yaml**。
启动/热重载时：effective = merge(config.yaml, overrides)。
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_overrides (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""


class ConfigStore:
    """配置覆盖的持久化。db_path 为空时退化为进程内存。"""

    def __init__(self, db_path: str | None = None):
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._memory: dict = {}
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def load(self) -> dict:
        with self._lock:
            if self._conn is None:
                return copy.deepcopy(self._memory)
            row = self._conn.execute("SELECT data FROM config_overrides WHERE id=1").fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0]) or {}
        except Exception:
            return {}

    def save(self, overrides: dict) -> None:
        with self._lock:
            if self._conn is None:
                self._memory = copy.deepcopy(overrides)
                return
            self._conn.execute(
                "INSERT INTO config_overrides(id, data) VALUES(1, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json.dumps(overrides, ensure_ascii=False),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def merge_config(base: dict, overrides: dict) -> dict:
    """把 overrides 叠加到 base 上，返回新字典（不修改 base）。"""
    result = copy.deepcopy(base or {})
    if not overrides:
        return result

    ov_providers = overrides.get("providers") or {}
    if isinstance(ov_providers, dict) and ov_providers:
        providers = result.setdefault("providers", {})
        for name, patch in ov_providers.items():
            if not isinstance(patch, dict):
                continue
            prov = providers.setdefault(name, {})
            prov.update(patch)

    ov_models = overrides.get("models") or []
    if isinstance(ov_models, list) and ov_models:
        models = result.setdefault("models", [])
        by_name = {m.get("name"): m for m in models if isinstance(m, dict)}
        for m in ov_models:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            if m["name"] in by_name:
                by_name[m["name"]].update(m)
            else:
                models.append(copy.deepcopy(m))
    return result
