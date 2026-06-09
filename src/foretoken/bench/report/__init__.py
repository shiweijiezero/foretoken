# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""评测产物组装:把一次回放结果写成可复现记录 + 人读摘要。

单 run 一目录:`run.json`(配置+指标)、`turns.jsonl`(每轮原始)、`cases.jsonl`(每轮输入输出)、
`summary.md`(markdown 摘要+内嵌图)、`en/`·`zh/`(双语图);跨 run `runs/INDEX.md` 排行榜。

分工:`metrics.py` 纯统计 · `plots.py` 出图 · `markdown.py` 文本渲染 · `writer.py` 写盘组装。
本模块只做再导出,聚合上述子模块的公开接口。
"""

from __future__ import annotations

from .compare import compare
from .markdown import rebuild_index, render_summary
from .metrics import goodput_ladder, gpu_rate, parse_slo, percentiles, summarize, throughput
from .plots import compare_runs, make_plots, regen_plots, sweep_curve
from .writer import regen_cases, write_run

__all__ = [
    "parse_slo", "summarize", "percentiles", "goodput_ladder", "throughput", "gpu_rate",
    "make_plots", "compare_runs", "compare", "sweep_curve", "regen_plots",
    "regen_cases", "render_summary", "write_run", "rebuild_index",
]
