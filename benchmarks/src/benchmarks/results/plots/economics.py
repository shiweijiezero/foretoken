# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Price saved token measurements and compare revenue, compute cost, and profit."""

from __future__ import annotations

import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from benchmarks.config.plot import DEFAULT_INTERACTIVITY, MetricSelection
from benchmarks.results.plots.data import _method, _metric_display, _numeric, _slug, paired_summaries, workload_slices
from benchmarks.results.plots.rendering import FigureWriter, _legend, _style


PRICED_TOKEN_METRICS = (
    "uncached_prompt_tokens_per_second_per_gpu",
    "cached_prompt_tokens_per_second_per_gpu",
    "generation_tokens_per_second_per_gpu",
)


class Pricing(BaseModel):
    """Validated token sale prices and GPU-hour costs for offline result plotting."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False, hide_input_in_errors=True)

    currency: str = "USD"
    input_per_million: float = Field(ge=0)
    cached_input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    gpu_hour_cost: float | dict[str, float]


def load_pricing(path: Path) -> Pricing:
    """Read an explicit YAML price scenario for the plot command, without fetching live prices."""
    try:
        prices = Pricing.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"Invalid pricing file {path}: {error}") from error
    costs = prices.gpu_hour_cost
    if isinstance(costs, dict):
        if not costs or any(not name.strip() or value < 0 for name, value in costs.items()):
            raise ValueError("gpu_hour_cost requires non-empty method names and non-negative costs")
    elif costs < 0:
        raise ValueError("gpu_hour_cost must be non-negative")
    if not prices.currency.strip():
        raise ValueError("pricing currency must not be empty")
    return prices


def price_metrics(metrics: dict[str, Any], method: str, pricing: Pricing) -> dict[str, float | None]:
    """Price one run before repeat aggregation, preserving missing token usage as unavailable."""
    costs = pricing.gpu_hour_cost
    if isinstance(costs, dict):
        if method not in costs:
            raise ValueError(f"No gpu_hour_cost configured for method {method!r}")
        cost = costs[method]
    else:
        cost = costs
    throughput = metrics.get("throughput", {})
    rates = tuple(_numeric(throughput.get(metric)) for metric in PRICED_TOKEN_METRICS)
    revenue = None
    if all(value is not None for value in rates):
        revenue = 3600 / 1_000_000 * sum(
            rate * price for rate, price in zip(rates, (
                pricing.input_per_million, pricing.cached_input_per_million, pricing.output_per_million,
            ))
        )
    return {
        "revenue_per_gpu_hour": revenue,
        "cost_per_gpu_hour": cost,
        "profit_per_gpu_hour": revenue - cost if revenue is not None else None,
    }


def render_economics(
    points: list[dict[str, Any]], summary: list[dict[str, Any]], writer: FigureWriter, pricing: Pricing,
    *, min_interactivity: float | None = None,
    interactivity: MetricSelection = DEFAULT_INTERACTIVITY, columns: int = 1,
) -> None:
    """Export measured operating-point choices and signed profit bars for each fixed workload.

    Each method contributes its highest mean-revenue point meeting the optional
    mean decode-speed floor for the selected metric. Revenue and speed use the
    same repeated runs; missing required values exclude that repetition from
    both statistics. Every point retains its selection status and repeat counts.
    """
    interactivity_key = interactivity.key
    point_by_key = {(_method(point), str(point["combination"])): point for point in points}
    metrics = (
        "revenue_per_gpu_hour", "cost_per_gpu_hour", "profit_per_gpu_hour", interactivity_key,
        "cached_prompt_tokens_per_second_per_gpu", "uncached_prompt_tokens_per_second_per_gpu",
        "generation_tokens_per_second_per_gpu",
    )
    coordinates: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in summary:
        if row["metric"] in metrics:
            coordinates[_method(row), str(row["combination"])][row["metric"]] = row
    required = ("revenue_per_gpu_hour", "cost_per_gpu_hour", "profit_per_gpu_hour")
    if min_interactivity is not None:
        required += (interactivity_key,)
    paired = paired_summaries(points, required)
    records = []
    for key, values in coordinates.items():
        point = point_by_key.get(key, {})
        reference = next(iter(values.values()))
        bench = reference.get("bench") or point.get("bench") or {}
        row = {
            "method": key[0], "combination": key[1],
            "max_concurrency": bench.get("max_concurrency", point.get("max_concurrency")),
            "currency": pricing.currency, "minimum_interactivity": min_interactivity,
            "interactivity_metric": interactivity.metric,
            "interactivity_stat": interactivity.stat,
            "interactivity_key": interactivity_key,
            "input_per_million": pricing.input_per_million,
            "cached_input_per_million": pricing.cached_input_per_million,
            "output_per_million": pricing.output_per_million,
            "bench": bench, "requested_runs": reference.get("requested_runs"),
            "failed_runs": reference.get("failed_runs"), "runs": reference.get("runs"),
            "paired_samples": paired.get(key, {}).get("revenue_per_gpu_hour", {}).get("samples", 0),
        }
        for metric in metrics:
            measured = paired.get(key, {}).get(metric, {})
            row[metric] = _numeric(measured.get("mean"))
            row[f"{metric}_stddev"] = _numeric(measured.get("stddev"))
            row[f"{metric}_samples"] = measured.get("samples", 0)
            row[f"{metric}_available_samples"] = values.get(metric, {}).get("samples", 0)
        if not values.get("revenue_per_gpu_hour", {}).get("samples"):
            row["status"] = "missing_pricing_metrics"
        elif min_interactivity is not None and not values.get(interactivity_key, {}).get("samples"):
            row["status"] = "missing_interactivity"
        elif not row["paired_samples"]:
            row["status"] = "no_paired_metrics"
        elif min_interactivity is not None and row[interactivity_key] < min_interactivity:
            row["status"] = "below_minimum_interactivity"
        else:
            row["status"] = "eligible"
        group = str(reference.get("parameter_group") or point.get("parameter_group") or "default")
        row["parameter_group"] = group
        records.append(row)
    plotted = False
    for index, (group, details, rows) in enumerate(workload_slices(records, "max_concurrency"), 1):
        selected = []
        for method in dict.fromkeys(row["method"] for row in rows):
            eligible = [row for row in rows if row["method"] == method and row["status"] == "eligible"]
            if eligible:
                best = max(eligible, key=lambda row: row["revenue_per_gpu_hour"])
                best["status"] = "selected"
                selected.append(best)
        base = f"economics-{index}-{_slug(group)}"
        writer.table(base, rows)
        for offset in range(0, len(selected), 6):
            panel = selected[offset:offset + 6]
            name = base + (f"-panel-{offset // 6 + 1}" if len(selected) > 6 else "")
            title = f"Revenue and cost · {group}" + (f"\n{details}" if details else "")
            _economics_figure(panel, writer, name, title, pricing, min_interactivity, interactivity_key, columns)
            plotted = True
    if not plotted:
        raise ValueError(
            "No measured operating point satisfies the economics requirements; "
            f"inspect economics CSV files in {writer.directory} for missing metrics or points below the speed floor"
        )


def _economics_figure(
    rows: list[dict[str, Any]], writer: FigureWriter, name: str, title: str, pricing: Pricing,
    minimum: float | None, interactivity_key: str, columns: int,
) -> None:
    """Draw cost plus positive profit, with losses below zero and a separate revenue marker."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(3.4 if columns == 1 else 7, 3.35), layout="constrained")
    FigureCanvasAgg(figure)
    axis = figure.add_subplot(111)
    positions = list(range(len(rows)))
    costs = [row["cost_per_gpu_hour"] for row in rows]
    profits = [row["profit_per_gpu_hour"] for row in rows]
    revenues = [row["revenue_per_gpu_hour"] for row in rows]
    axis.bar(positions, costs, width=0.64, color="#b9c5cd", label="Compute cost")
    if any(value >= 0 for value in profits):
        axis.bar(positions, [max(0, value) for value in profits], bottom=costs,
                 width=0.64, color="#31834d", label="Profit")
    if any(value < 0 for value in profits):
        axis.bar(positions, [min(0, value) for value in profits], width=0.64,
                 color="#c84445", label="Loss")
    axis.scatter(positions, revenues, color="#0b0b0b", marker="_", s=230, linewidths=1.4,
                 zorder=4, label="Revenue")
    for position, row in enumerate(rows):
        revenue = row["revenue_per_gpu_hour"]
        error = row["revenue_per_gpu_hour_stddev"]
        if error is not None:
            axis.errorbar(position, revenue, yerr=error, fmt="none", color="#0b0b0b", capsize=3, linewidth=0.8)
        axis.annotate(f"{revenue:.2f}", (position, revenue), xytext=(0, 7),
                      textcoords="offset points", ha="center", fontsize=8)
        profit = row["profit_per_gpu_hour"]
        if profit:
            midpoint = row["cost_per_gpu_hour"] + profit / 2 if profit > 0 else profit / 2
            axis.text(position, midpoint, f"{profit:+.2f}", ha="center", va="center", fontsize=8,
                      color="white")
    labels = []
    for row in rows:
        label = textwrap.fill(row["method"], width=16 if columns == 1 else 24)
        if row["max_concurrency"] is not None:
            label += f"\nc{row['max_concurrency']}"
        speed = row[interactivity_key]
        if speed is not None:
            label += f" · {speed:.1f} tok/s/user"
        labels.append(label)
    axis.set_xticks(positions, labels)
    axis.axhline(0, linewidth=0.7, color="#52514e")
    if minimum is not None:
        title += f"\n{_metric_display(interactivity_key)[1]} ≥ {minimum:g}"
    elif any(row[interactivity_key] is not None for row in rows):
        title += f"\n{_metric_display(interactivity_key)[1]}"
    _style(axis, title, "Measured operating point", f"{pricing.currency} / GPU-hour")
    axis.margins(y=0.2)
    _legend(figure, axis, columns)
    writer.figure(figure, name)
