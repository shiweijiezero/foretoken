# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project native quality and paired model comparison scores onto chart coordinates."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from benchmarks.results.plots.data import Chart, Series, _numeric


def comparison_charts(metrics: dict[str, Any]) -> tuple[list[Chart], list[str]] | None:
    """Read comparison charts and method identities for standalone and experiment exports."""
    if "evaluation_comparison" in metrics:
        comparison = metrics["evaluation_comparison"]
        return _evaluation_comparison_charts(comparison), [method["label"] for method in comparison["methods"]]
    if "greedy_comparison" in metrics:
        comparison = metrics["greedy_comparison"]
        return _greedy_charts(comparison), list(dict.fromkeys(str(row["method"]) for row in comparison["candidates"]))
    if "distribution_comparison" in metrics:
        comparison = metrics["distribution_comparison"]
        return _distribution_charts(comparison), list(dict.fromkeys(str(row["method"]) for row in comparison["candidates"]))
    return None


def _quality_charts(metrics: dict[str, Any]) -> list[Chart]:
    """Separate native evaluator scores by metric, filter, level and display unit."""
    grouped: dict[tuple[str, str, str, str, float], list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in metrics["scores"]:
        multiplier = _numeric(row.get("display_multiplier")) or 1.0
        if _numeric(row.get("value")) is not None:
            grouped[
                (
                    str(row["metric"]),
                    str(row["filter"]),
                    str(row["level"]),
                    str(row.get("display_unit") or ""),
                    multiplier,
                )
            ].append(row)
    charts = []
    for (metric, filter_name, level, unit, multiplier), rows in grouped.items():
        # One chart per native score identity; stderr remains the evaluator's
        # stderr, not a deviation computed from different task samples.
        for offset in range(0, len(rows), 8):
            batch = rows[offset : offset + 8]
            labels = tuple(
                f"{row['task']}/{row['subset']}"
                if row.get("subset")
                else str(row["task"])
                for row in batch
            )
            charts.append(
                Chart(
                    name="quality-"
                    + re.sub(
                        r"[^a-z0-9]+",
                        "-",
                        f"{metric}-{filter_name}-{level}-{unit}-{offset}".lower(),
                    ).strip("-"),
                    title=f"{metric} · {filter_name or 'all'} · {level}",
                    xlabel="Task / subset",
                    ylabel=f"{metric} ({unit})" if unit else metric,
                    series=(
                        Series(
                            "Native score",
                            tuple(float(i) for i in range(len(batch))),
                            tuple(float(row["value"]) * multiplier for row in batch),
                            tuple(
                                float(row["stderr"]) * multiplier
                                if _numeric(row.get("stderr")) is not None
                                else None
                                for row in batch
                            ),
                            tuple(
                                {
                                    **row,
                                    "error_type": "native stderr"
                                    if row.get("stderr") is not None
                                    else "",
                                }
                                for row in batch
                            ),
                        ),
                    ),
                    tick_labels=labels,
                    kind="scatter",
                    metric=metric,
                )
            )
    return charts


def _distribution_charts(comparison: dict[str, Any]) -> list[Chart]:
    """Use already-scored candidate and position values, never reconstructed probabilities."""
    points, positions = comparison["candidates"], comparison["positions"]
    charts = []
    axes = [
        name
        for name in ("weight_bits", "bits_per_weight", "model_size_gib")
        if any(_numeric(row.get(name)) is not None for row in points)
    ] or [""]
    for axis in axes:
        for field in (
            "mean_kl",
            "p99_kl",
            "mean_centered_logit_rmse",
            "top1_agreement",
        ):
            selected = [
                row
                for row in points
                if _numeric(row.get(field)) is not None
                and (not axis or _numeric(row.get(axis)) is not None)
            ]
            if not selected:
                continue
            methods = list(dict.fromkeys(str(row["method"]) for row in points))
            series = []
            for method in methods:
                batch = [row for row in selected if row["method"] == method]
                if not batch:
                    continue
                coords = [
                    (float(row[axis]) if axis else float(points.index(row)), row)
                    for row in batch
                ]
                coords.sort(key=lambda pair: pair[0])
                factor = 100 if field == "top1_agreement" else 1
                series.append(
                    Series(
                        method,
                        tuple(x for x, _ in coords),
                        tuple(row[field] * factor for _, row in coords),
                        (None,) * len(coords),
                        tuple(row for _, row in coords),
                    )
                )
            charts.append(
                Chart(
                    f"distribution-{field}-{axis or 'candidates'}",
                    f"Candidate comparison · {field}",
                    {
                        "weight_bits": "Nominal weight precision (bits)",
                        "bits_per_weight": "Effective bits per weight",
                        "model_size_gib": "Checkpoint size (GiB)",
                    }.get(axis, "Candidate"),
                    field.replace("_", " ")
                    + (" (%)" if factor == 100 else " (nats)" if "kl" in field else ""),
                    tuple(series),
                    tick_labels=tuple(row["label"] for row in points)
                    if not axis
                    else (),
                    kind="pareto" if axis and field == "mean_kl" else "scatter",
                    yscale="symlog" if field in {"mean_kl", "p99_kl"} else "linear",
                    metric=field,
                )
            )
    for field in ("kl", "centered_logit_rmse"):
        series = []
        for point in points:
            rows = sorted(
                (
                    row
                    for row in positions
                    if row["candidate"] == point["label"]
                    and _numeric(row.get(field)) is not None
                ),
                key=lambda row: row["scored_index"],
            )
            if rows:
                series.append(
                    Series(
                        point["label"],
                        tuple(float(row["scored_index"]) for row in rows),
                        tuple(float(row[field]) for row in rows),
                        (None,) * len(rows),
                        tuple({**row, "method": point["method"]} for row in rows),
                    )
                )
        if series:
            charts.append(
                Chart(
                    f"distribution-positions-{field}",
                    f"Scored positions · {field}",
                    "Scored position index (not time)",
                    field + (" (nats)" if field == "kl" else ""),
                    tuple(series),
                    yscale="symlog" if field == "kl" else "linear",
                    metric=field,
                )
            )
    return charts


def _evaluation_comparison_charts(comparison: dict[str, Any]) -> list[Chart]:
    """Keep every task, subset, filter, and metric in its own candidate chart."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in comparison["scores"]:
        if _numeric(row.get("value")) is not None:
            identity = tuple(str(row.get(key) or "") for key in ("task", "level", "subset", "filter", "metric", "display_unit"))
            grouped[identity].append(row)
    methods = [method["label"] for method in comparison["methods"]]
    charts = []
    for index, (identity, rows) in enumerate(grouped.items(), 1):
        task, _level, subset, filter_name, metric, unit = identity
        factor = float(rows[0].get("display_multiplier") or 1)
        series = tuple(Series(
            row["method"], (float(methods.index(row["method"])),),
            (float(row["value"]) * factor,),
            (float(row["stderr"]) * factor if _numeric(row.get("stderr")) is not None else None,),
            (row,),
        ) for row in rows)
        charts.append(Chart(
            f"evaluation-comparison-{index}-{re.sub('[^a-z0-9]+', '-', metric.lower()).strip('-')[:40]}",
            " · ".join(part for part in (task, subset, filter_name, metric) if part),
            "Deployment", f"{metric} ({unit})" if unit else metric,
            series, tick_labels=tuple(methods), kind="scatter", metric=metric,
        ))
    return charts


def _greedy_charts(comparison: dict[str, Any]) -> list[Chart]:
    """Plot saved exact-match rates and divergence positions without counting failed requests."""
    points = comparison["candidates"]
    charts = []
    selected = [(index, row) for index, row in enumerate(points) if row["exact_match_rate"] is not None]
    if selected:
        charts.append(Chart(
            "greedy-exact-match", "Greedy exact match", "Candidate",
            "Exact sequence match (%)",
            tuple(Series(
                row["label"], (float(index),), (row["exact_match_rate"] * 100,),
                (None,), (row,),
            ) for index, row in selected),
            tick_labels=tuple(row["label"] for row in points), kind="scatter",
            metric="exact_match_rate",
        ))
    divergence = []
    for point in points:
        rows = [row for row in comparison["samples"]
                if row["candidate"] == point["label"] and row["first_divergence"] is not None]
        if rows:
            divergence.append(Series(
                point["label"],
                tuple(float(row["window"]) for row in rows),
                tuple(float(row["first_divergence"]) for row in rows),
                (None,) * len(rows),
                tuple({**row, "method": point["method"]} for row in rows),
            ))
    if divergence:
        charts.append(Chart(
            "greedy-first-divergence", "First differing generated token by sample",
            "Sample index", "First differing token position", tuple(divergence),
            kind="scatter", metric="first_divergence",
        ))
    return charts
