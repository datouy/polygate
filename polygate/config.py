"""配置文件加载与环境变量展开。"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path
from typing import Any

import yaml

_ENV_PAT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, missing: set[str] | None = None) -> Any:
    """递归展开 ``${NAME}``；缺失的变量名收集到 ``missing`` 并替换为空串。"""
    if missing is None:
        missing = set()
    if isinstance(value, str):

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name in os.environ:
                return os.environ[name]
            missing.add(name)
            return ""

        return _ENV_PAT.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, missing) for v in value]
    return value


def load_config(path: str | Path) -> dict:
    """读取并展开环境变量；缺失变量会发出警告而不是静默降级。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    missing: set[str] = set()
    cfg = _expand_env(raw, missing)
    for name in sorted(missing):
        warnings.warn(
            f"环境变量未设置：{name}（对应 ${{{name}}} 将被替换为空字符串，"
            f"相关 Provider 可能因缺少 Key 而无法路由）",
            RuntimeWarning,
            stacklevel=2,
        )
    return cfg
