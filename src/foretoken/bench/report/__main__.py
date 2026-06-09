# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""CLI:重画图 / 重建 cases / 负载扫描招牌图 / 多组对比 / 计价成本 / 重建 INDEX。

  python -m foretoken.bench.report [runs_dir] [--plots] [--cases]
  python -m foretoken.bench.report --compare <run_dir> <run_dir> ...   (或 runs/ 比全部)
  python -m foretoken.bench.report --cost <run_dir> ... [--gpu-rate A100=8 --slo-tier 0]
  python -m foretoken.bench.report --sweep [--x rate|total|window] <run_dir> ...
"""

from __future__ import annotations

import sys
from pathlib import Path

from foretoken.bench.report import (
    compare,
    cost_report,
    rebuild_index,
    regen_cases,
    regen_plots,
    sweep_curve,
)

argv = sys.argv[1:]
flags = {a for a in argv if a.startswith("-")}
pos = [a for a in argv if not a.startswith("-")]


def _opt(name: str, default: str) -> str:
    """取 --name <value>;缺省返回 default。"""
    i = argv.index(name) if name in argv else -1
    return argv[i + 1] if 0 <= i < len(argv) - 1 else default


if "--sweep" in flags:  # 负载扫描:把列出的 run 画成 goodput-vs-load 曲线
    x = _opt("--x", "rate")
    print(f"扫描招牌图(x={x}):{sweep_curve([a for a in pos if a != x], x_key=x)}")
    sys.exit(0)

if "--compare" in flags:  # 多组实验对比:全指标表 + 对比图 → <out>/summary.md
    out = _opt("--out", "")
    dirs = [a for a in pos if a != out] or ["results/runs"]
    res = compare(dirs, out_dir=out or None)
    print(f"对比写入:{res / 'summary.md'}" if res else "对比需 ≥2 个 run(检查路径)")
    sys.exit(0)

if "--cost" in flags:  # 计价成本:各配置 $/Mtok(原始 / 达成 SLO)→ <out>/cost.md
    out = _opt("--out", "")
    tier_s = _opt("--slo-tier", "0")
    rate_vals = [argv[i + 1] for i, a in enumerate(argv) if a == "--gpu-rate" and i + 1 < len(argv)]
    rates = {k: float(v) for k, _, v in (rv.partition("=") for rv in rate_vals) if v}
    consumed = {out, tier_s, *rate_vals}
    dirs = [a for a in pos if a not in consumed] or ["results/runs"]
    res = cost_report(dirs, out_dir=out or None, rates=rates, slo_tier=int(tier_s))
    print(f"成本报告写入:{res / 'cost.md'}" if res else "无可读 run(检查路径)")
    sys.exit(0)

d = pos[0] if pos else "results/runs"
if "--plots" in flags:  # 给所有 run 重画图(en+zh)
    for tj in sorted(Path(d).glob("*/turns.jsonl")):
        regen_plots(tj.parent)
    print(f"已重画各 run 图(en+zh):{d}")
if "--cases" in flags:  # 从 cases.jsonl 重建各 run 的 cases.md
    n = sum(regen_cases(cj.parent) for cj in sorted(Path(d).glob("*/cases.jsonl")))
    print(f"已重建 cases.md:{n} 个 run")
rebuild_index(d)
print(f"INDEX 重建:{d}/INDEX.md")
