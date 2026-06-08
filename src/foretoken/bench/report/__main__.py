"""CLI:重画图 / 出对照图 / 重建 INDEX。

  python -m foretoken.bench.report [runs_dir] [--plots] [--compare]
"""

from __future__ import annotations

import sys
from pathlib import Path

from foretoken.bench.report import compare_runs, rebuild_index, regen_plots

argv = [a for a in sys.argv[1:] if not a.startswith("-")]
d = argv[0] if argv else "runs"
if "--plots" in sys.argv:  # 给所有 run 重画图(en+zh)
    for tj in sorted(Path(d).glob("*/turns.jsonl")):
        regen_plots(tj.parent)
    print(f"已重画各 run 图(en+zh):{d}")
if "--compare" in sys.argv:  # 跨 run 对照图(en+zh)
    print(f"对照图:{compare_runs(d)}")
rebuild_index(d)
print(f"INDEX 重建:{d}/INDEX.md")
