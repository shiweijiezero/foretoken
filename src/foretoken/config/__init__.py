# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""配置加载:模型的 `[sampling]` + `[serve]` 两段 toml(`config/models/*.toml`),见 docs/14。

`replay --config <path>` 直接指定文件(任意路径);缺省回退 `config/models/default.toml`。
采样喂回放、serve 配置喂引擎,故置于顶层(不专属 bench)。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_DEFAULT = Path(__file__).parents[3] / "config" / "models" / "default.toml"


def resolve(config: str | None = None) -> Path:
    """配置文件路径:显式 `config` 文件(任意路径)优先,否则 `config/models/default.toml`。"""
    return Path(config) if config else _DEFAULT


def read(path: str | Path, kind: str = "sampling") -> dict:
    """读 toml 文件的 `[kind]` 段(`sampling` / `serve`);文件不存在 → `{}`。"""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        return dict(tomllib.load(fh).get(kind, {}))
