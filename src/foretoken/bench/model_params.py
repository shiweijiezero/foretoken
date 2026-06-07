"""读 `config/models/<model>.toml`:每模型一文件(采样 + serve),见 docs/14。"""

from __future__ import annotations

import tomllib
from pathlib import Path

_DIR = Path(__file__).parents[3] / "config" / "models"


def params_for(model: str, kind: str = "sampling") -> dict:
    """按 `config/models/<name>.toml` 文件名 substring 匹配模型;无匹配回退 default.toml。"""
    m = model.lower()
    default: dict = {}
    for f in sorted(_DIR.glob("*.toml")):
        with open(f, "rb") as fh:
            cfg = tomllib.load(fh)
        if f.stem == "default":
            default = cfg.get(kind, {})
        elif f.stem.lower() in m:
            return dict(cfg.get(kind, {}))
    return dict(default)
