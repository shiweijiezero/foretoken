# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse offline figure selections without loading measurement or plotting backends."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TradeoffOptions:
    """Select two measured axes and the workload parameter joining their points."""

    x_metric: str = "generation_tokens_per_second_per_user"
    y_metric: str = "generation_tokens_per_second_per_gpu"
    sweep_axis: str = "max_concurrency"
    label_by: tuple[str, ...] = ("max_concurrency",)
    label_values: bool = False
    compare_axis: str = "x"
    frontier: str = "max-max"


@dataclass(frozen=True)
class PlotOptions:
    """A validated command passed from argument parsing to the saved-result exporter."""

    sources: tuple[Path, ...]
    output_dir: Path | None
    columns: int
    metrics: tuple[str, ...]
    methods: tuple[str, ...]
    base: Path | None
    tradeoff: TradeoffOptions | None
    pricing: Path | None
    min_interactivity: float | None
    gpu_count: int | None
    sync_wandb: bool


def parse_plot_arguments(argv: Sequence[str] | None = None) -> PlotOptions:
    """Parse figure choices and require the inputs used by optional financial charts."""
    parser = argparse.ArgumentParser(
        prog="foretoken plot",
        description="Export PDF, SVG, PNG, and CSV from saved benchmark results.",
    )
    parser.add_argument("sources", type=Path, nargs="+", metavar="RESULT_DIR",
                        help="Result directories or folders containing saved results")
    parser.add_argument("--output-dir", type=Path,
                        help="Figure directory (default: ./plots for comparisons, RESULT_DIR/plots otherwise)")
    parser.add_argument("--columns", type=int, choices=(1, 2), default=1,
                        help="Paper column width (default: 1)")
    parser.add_argument("--metric", action="extend", nargs="+", default=[],
                        help="Metrics to include in ordinary sweep and comparison figures")
    parser.add_argument("--method", action="extend", nargs="+", default=[],
                        help="Named methods to include")
    parser.add_argument("--base", type=Path, metavar="RESULT_DIR",
                        help="Reference result or iteration for matched-workload changes")
    axes = TradeoffOptions()
    parser.add_argument("--x-metric", help=f"Trade-off x metric (default: {axes.x_metric})")
    parser.add_argument("--y-metric", help=f"Trade-off y metric (default: {axes.y_metric})")
    parser.add_argument("--sweep-axis", help="Workload parameter allowed to vary along each curve (default: max_concurrency)")
    parser.add_argument("--label-by", nargs="+", metavar="FIELD",
                        help="Workload fields to label at each point (default: max_concurrency)")
    parser.add_argument("--label-values", action="store_true", help="Also label each trade-off point with its x/y values")
    parser.add_argument("--compare-axis", choices=("x", "y"),
                        help="Axis for percentage changes against --base (default: x)")
    parser.add_argument("--frontier", choices=("none", "max-max", "min-max", "max-min", "min-min"),
                        help="Connect Pareto-optimal points for x/y objectives (default: max-max for default axes, none for custom axes)")
    parser.add_argument("--pricing", type=Path, metavar="YAML",
                        help="Token prices and GPU-hour costs for revenue, cost, and profit figures")
    parser.add_argument("--min-interactivity", type=float, metavar="TOKENS_PER_SECOND",
                        help="Minimum TPOT-based P90 decode speed when selecting revenue points")
    parser.add_argument("--gpu-count", type=int,
                        help="GPU capacity for saved results that did not record it")
    parser.add_argument("--sync_wandb", action="store_true",
                        help="Also synchronize saved results and per-run figures to their original W&B runs")
    args = parser.parse_args(argv)
    if args.min_interactivity is not None:
        if args.pricing is None or not math.isfinite(args.min_interactivity) or args.min_interactivity < 0:
            parser.error("--min-interactivity requires --pricing and a finite nonnegative speed")
    if args.gpu_count is not None and args.gpu_count < 1:
        parser.error("--gpu-count must be a positive integer")
    if args.compare_axis is not None and args.base is None:
        parser.error("--compare-axis requires --base")
    selected = any((args.x_metric, args.y_metric, args.sweep_axis, args.label_by,
                    args.label_values, args.compare_axis, args.frontier))
    tradeoff = TradeoffOptions(
        x_metric=args.x_metric or axes.x_metric,
        y_metric=args.y_metric or axes.y_metric,
        sweep_axis=args.sweep_axis or axes.sweep_axis,
        label_by=tuple(args.label_by) if args.label_by is not None else axes.label_by,
        label_values=args.label_values,
        compare_axis=args.compare_axis or axes.compare_axis,
        frontier=args.frontier or ("none" if args.x_metric or args.y_metric else axes.frontier),
    ) if selected else None
    return PlotOptions(
        sources=tuple(args.sources), output_dir=args.output_dir, columns=args.columns,
        metrics=tuple(args.metric), methods=tuple(args.method), base=args.base,
        tradeoff=tradeoff, pricing=args.pricing, min_interactivity=args.min_interactivity,
        gpu_count=args.gpu_count, sync_wandb=args.sync_wandb,
    )
