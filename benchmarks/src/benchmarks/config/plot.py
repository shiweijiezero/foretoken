# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse offline figure selections without loading measurement or plotting backends."""

from __future__ import annotations

import argparse
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INTERACTIVITY_METRIC = "decode_interactivity_p90"


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
    interactivity_metric: str
    gpu_count: int | None
    sync_wandb: bool
    progress: bool


def decode_interactivity_percentile(metric: str) -> float | None:
    """Resolve a decode-speed metric's TPOT percentile for CLI, calculations, and labels.

    Other metric families return None. Decode-speed selectors accept decimal
    percentiles from 0 through 100, including fractional tail percentiles.
    """
    prefix = "decode_interactivity_p"
    if not metric.startswith(prefix):
        return None
    suffix = metric.removeprefix(prefix)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", suffix):
        raise ValueError(f"Invalid decode-speed metric {metric!r}; use decode_interactivity_p<N> with 0 <= N <= 100")
    percentile = float(suffix)
    if not 0 <= percentile <= 100:
        raise ValueError(f"Decode-speed percentile must be between 0 and 100, got {suffix}")
    return percentile


def parse_plot_arguments(argv: Sequence[str] | None = None) -> PlotOptions:
    """Select saved results or report progress data and validate their applicable options."""
    parser = argparse.ArgumentParser(
        prog="foretoken plot",
        description="Export figures and tables from saved benchmark results or report progress data.",
    )
    parser.add_argument("sources", type=Path, nargs="*", metavar="RESULT_DIR",
                        help="Result directories or folders containing saved results")
    parser.add_argument("--progress", type=Path, nargs="+", action="extend", default=[], metavar="YAML",
                        help="Render report milestone and search-history figures from progress data instead")
    parser.add_argument("--output-dir", type=Path,
                        help="Figure directory (default: RESULT_DIR/plots; beside progress files; ./plots for comparisons)")
    parser.add_argument("--columns", type=int, choices=(1, 2),
                        help="Saved-result paper column width (default: 1)")
    parser.add_argument("--metric", action="extend", nargs="+", default=[],
                        help="Request-level or run-summary metrics to plot")
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
                        help="Minimum decode speed under --interactivity-metric when selecting revenue points")
    parser.add_argument("--interactivity-metric", metavar="METRIC",
                        help=f"Decode-speed percentile used for revenue selection (default: {DEFAULT_INTERACTIVITY_METRIC})")
    parser.add_argument("--gpu-count", type=int,
                        help="GPU capacity for saved results that did not record it")
    parser.add_argument("--sync_wandb", action="store_true",
                        help="Also synchronize saved results and per-run figures to their original W&B runs")
    args = parser.parse_args(argv)
    if bool(args.sources) == bool(args.progress):
        parser.error("provide result directories or --progress YAML files, but not both")
    selected = any((args.x_metric, args.y_metric, args.sweep_axis, args.label_by,
                    args.label_values, args.compare_axis, args.frontier))
    if args.progress and (
        selected or args.columns is not None or args.metric or args.method or args.base
        or args.pricing or args.min_interactivity is not None or args.interactivity_metric is not None
        or args.gpu_count is not None or args.sync_wandb
    ):
        parser.error("--progress accepts --output-dir; metric, baseline and comparison settings come from the YAML files")
    interactivity_metric = DEFAULT_INTERACTIVITY_METRIC if args.interactivity_metric is None else args.interactivity_metric
    try:
        for metric in (*args.metric, args.x_metric, args.y_metric):
            if metric is not None:
                decode_interactivity_percentile(metric)
        if decode_interactivity_percentile(interactivity_metric) is None:
            parser.error("--interactivity-metric requires decode_interactivity_p<N>")
    except ValueError as error:
        parser.error(str(error))
    if args.interactivity_metric is not None and args.pricing is None:
        parser.error("--interactivity-metric requires --pricing")
    if args.min_interactivity is not None:
        if args.pricing is None or not math.isfinite(args.min_interactivity) or args.min_interactivity < 0:
            parser.error("--min-interactivity requires --pricing and a finite nonnegative speed")
    if args.gpu_count is not None and args.gpu_count < 1:
        parser.error("--gpu-count must be a positive integer")
    if args.compare_axis is not None and args.base is None:
        parser.error("--compare-axis requires --base")
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
        sources=tuple(args.progress or args.sources), output_dir=args.output_dir, columns=args.columns or 1,
        metrics=tuple(args.metric), methods=tuple(args.method), base=args.base,
        tradeoff=tradeoff, pricing=args.pricing, min_interactivity=args.min_interactivity,
        interactivity_metric=interactivity_metric,
        gpu_count=args.gpu_count, sync_wandb=args.sync_wandb, progress=bool(args.progress),
    )
