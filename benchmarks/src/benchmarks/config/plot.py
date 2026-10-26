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


@dataclass(frozen=True)
class MetricSelection:
    """A metric and optional request statistic selected by a plotting caller."""

    metric: str
    stat: str | None = None

    @property
    def key(self) -> str:
        """Resolve the selection to the existing saved or derived metric field."""
        if not self.metric:
            raise ValueError("Metric name must not be empty")
        if self.stat is None:
            if self.metric == "decode_interactivity":
                raise ValueError("decode_interactivity requires a percentile statistic such as p95")
            decode_interactivity_percentile(self.metric)
            return self.metric
        if self.metric == "decode_interactivity":
            _percentile_value(self.stat)
            return f"{self.metric}_{self.stat}"
        if self.metric in {"latency", "ttft", "tpot", "itl"}:
            if self.stat != "mean":
                _percentile_value(self.stat)
            return f"{self.metric}_{self.stat}_seconds"
        raise ValueError(f"Metric {self.metric!r} does not accept a statistic; select its saved field directly")


DEFAULT_INTERACTIVITY = MetricSelection("decode_interactivity", "p90")


@dataclass(frozen=True)
class TradeoffOptions:
    """Select two measured axes and the workload parameter joining their points."""

    x: MetricSelection = MetricSelection("generation_tokens_per_second_per_user")
    y: MetricSelection = MetricSelection("generation_tokens_per_second_per_gpu")
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
    metrics: tuple[MetricSelection, ...]
    methods: tuple[str, ...]
    base: Path | None
    tradeoff: TradeoffOptions | None
    pricing: Path | None
    min_interactivity: float | None
    interactivity: MetricSelection
    gpu_count: int | None
    sync_wandb: bool


def _percentile_value(stat: str) -> float:
    """Read the shared percentile syntax used by metric selections and saved keys."""
    match = re.fullmatch(r"p([0-9]+(?:\.[0-9]+)?)", stat)
    if match is None:
        raise ValueError(f"Expected percentile statistic p<N> with 0 <= N <= 100, got {stat!r}")
    percentile = float(match[1])
    if not 0 <= percentile <= 100:
        raise ValueError(f"Percentile must be between 0 and 100, got {match[1]}")
    return percentile


def decode_interactivity_percentile(metric: str) -> float | None:
    """Resolve a saved decode-speed field's TPOT percentile, or None for other metrics."""
    prefix = "decode_interactivity_p"
    if not metric.startswith(prefix):
        return None
    return _percentile_value("p" + metric.removeprefix(prefix))


def _metric_selections(metrics: Sequence[str], stats: Sequence[str]) -> tuple[MetricSelection, ...]:
    """Pair selections by position, broadcasting a single metric or statistic."""
    if not stats:
        return tuple(MetricSelection(metric) for metric in metrics)
    if not metrics:
        raise ValueError("--stat requires --metric")
    if len(metrics) == 1:
        return tuple(MetricSelection(metrics[0], stat) for stat in stats)
    if len(stats) == 1:
        return tuple(MetricSelection(metric, stats[0]) for metric in metrics)
    if len(metrics) != len(stats):
        raise ValueError("--metric and --stat require equal counts, or a single metric or statistic to broadcast")
    return tuple(MetricSelection(metric, stat) for metric, stat in zip(metrics, stats))


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
                        help="Request-level or run-summary metrics to plot")
    parser.add_argument("--stat", action="extend", nargs="+", default=[],
                        help="Statistics for --metric: mean or p<N>; pair by position, broadcasting a single value")
    parser.add_argument("--method", action="extend", nargs="+", default=[],
                        help="Named methods to include")
    parser.add_argument("--base", type=Path, metavar="RESULT_DIR",
                        help="Reference result or iteration for matched-workload changes")
    axes = TradeoffOptions()
    parser.add_argument("--x-metric", help=f"Trade-off x metric (default: {axes.x.metric})")
    parser.add_argument("--x-stat", help="Statistic for the trade-off x metric: mean or p<N>")
    parser.add_argument("--y-metric", help=f"Trade-off y metric (default: {axes.y.metric})")
    parser.add_argument("--y-stat", help="Statistic for the trade-off y metric: mean or p<N>")
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
                        help="Minimum decode speed under the selected interactivity metric and statistic")
    parser.add_argument("--interactivity-metric", metavar="METRIC",
                        help=f"Metric used for revenue selection (default: {DEFAULT_INTERACTIVITY.metric})")
    parser.add_argument("--interactivity-stat", metavar="STAT",
                        help=f"Decode-speed percentile used for revenue selection (default: {DEFAULT_INTERACTIVITY.stat})")
    parser.add_argument("--gpu-count", type=int,
                        help="GPU capacity for saved results that did not record it")
    parser.add_argument("--sync_wandb", action="store_true",
                        help="Also synchronize saved results and per-run figures to their original W&B runs")
    args = parser.parse_args(argv)
    x = MetricSelection(axes.x.metric if args.x_metric is None else args.x_metric, args.x_stat)
    y = MetricSelection(axes.y.metric if args.y_metric is None else args.y_metric, args.y_stat)
    interactivity = MetricSelection(
        DEFAULT_INTERACTIVITY.metric if args.interactivity_metric is None else args.interactivity_metric,
        DEFAULT_INTERACTIVITY.stat if args.interactivity_stat is None else args.interactivity_stat,
    )
    try:
        metrics = _metric_selections(args.metric, args.stat)
        for selection in (*metrics, x, y, interactivity):
            _ = selection.key
        if interactivity.metric != DEFAULT_INTERACTIVITY.metric:
            parser.error("--interactivity-metric requires decode_interactivity")
    except ValueError as error:
        parser.error(str(error))
    if (args.interactivity_metric is not None or args.interactivity_stat is not None) and args.pricing is None:
        parser.error("--interactivity-metric and --interactivity-stat require --pricing")
    if args.min_interactivity is not None:
        if args.pricing is None or not math.isfinite(args.min_interactivity) or args.min_interactivity < 0:
            parser.error("--min-interactivity requires --pricing and a finite nonnegative speed")
    if args.gpu_count is not None and args.gpu_count < 1:
        parser.error("--gpu-count must be a positive integer")
    if args.compare_axis is not None and args.base is None:
        parser.error("--compare-axis requires --base")
    selected = any((args.x_metric is not None, args.x_stat is not None,
                    args.y_metric is not None, args.y_stat is not None,
                    args.sweep_axis, args.label_by, args.label_values, args.compare_axis, args.frontier))
    custom_axes = any(value is not None for value in (args.x_metric, args.x_stat, args.y_metric, args.y_stat))
    tradeoff = TradeoffOptions(
        x=x, y=y,
        sweep_axis=args.sweep_axis or axes.sweep_axis,
        label_by=tuple(args.label_by) if args.label_by is not None else axes.label_by,
        label_values=args.label_values,
        compare_axis=args.compare_axis or axes.compare_axis,
        frontier=args.frontier or ("none" if custom_axes else axes.frontier),
    ) if selected else None
    return PlotOptions(
        sources=tuple(args.sources), output_dir=args.output_dir, columns=args.columns,
        metrics=metrics, methods=tuple(args.method), base=args.base,
        tradeoff=tradeoff, pricing=args.pricing, min_interactivity=args.min_interactivity,
        interactivity=interactivity, gpu_count=args.gpu_count, sync_wandb=args.sync_wandb,
    )
