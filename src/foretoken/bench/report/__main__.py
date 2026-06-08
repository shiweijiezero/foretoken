# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""CLI:重画图 / 出对照图 / 负载扫描招牌图 / 重建 INDEX。

  python -m foretoken.bench.report [runs_dir] [--plots] [--cases] [--compare]
  python -m foretoken.bench.report --sweep [--x rate|total|window] <run_dir> <run_dir> ...
"""

from __future__ import annotations

import sys
from pathlib import Path

from foretoken.bench.report import (
    compare_runs,
    rebuild_index,
    regen_cases,
    regen_plots,
    sweep_curve,
)

argv = sys.argv[1:]
flags = {a for a in argv if a.startswith("-")}


def _opt(name: str, default: str) -> str:
    """取 --name <value>;缺省返回 default。"""
    i = argv.index(name) if name in argv else -1
    return argv[i + 1] if 0 <= i < len(argv) - 1 else default


if "--sweep" in flags:  # 负载扫描:把列出的 run 画成 goodput-vs-load 曲线
    x = _opt("--x", "rate")
    dirs = [a for a in argv if not a.startswith("-") and a != x]
    print(f"扫描招牌图(x={x}):{sweep_curve(dirs, x_key=x)}")
    sys.exit(0)

pos = [a for a in argv if not a.startswith("-")]
d = pos[0] if pos else "runs"
if "--plots" in flags:  # 给所有 run 重画图(en+zh)
    for tj in sorted(Path(d).glob("*/turns.jsonl")):
        regen_plots(tj.parent)
    print(f"已重画各 run 图(en+zh):{d}")
if "--cases" in flags:  # 从 cases.jsonl 重建各 run 的 cases.md
    n = sum(regen_cases(cj.parent) for cj in sorted(Path(d).glob("*/cases.jsonl")))
    print(f"已重建 cases.md:{n} 个 run")
if "--compare" in flags:  # 跨 run 对照图(en+zh)
    print(f"对照图:{compare_runs(d)}")
rebuild_index(d)
print(f"INDEX 重建:{d}/INDEX.md")
