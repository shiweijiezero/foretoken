# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Describe sweep figures from persisted run summaries without recomputing statistics."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Series:
    """One method's ordered coordinates and the summary rows behind its error bars."""

    name: str
    x: tuple[float, ...]
    y: tuple[float, ...]
    stddev: tuple[float | None, ...]
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Chart:
    """A single-metric, single-axis figure shared by local rendering and W&B."""

    name: str
    title: str
    xlabel: str
    ylabel: str
    series: tuple[Series, ...]
    tick_labels: tuple[str, ...] = ()
    kind: str = "line"
    yscale: str = "linear"
    metric: str = ""


def _method(row: dict[str, Any], point: dict[str, Any] | None = None) -> str:
    """Use the service display name, not a result directory or endpoint URL, as method identity."""
    bench = row.get("bench") or (point or {}).get("bench") or {}
    service = bench.get("service") or {}
    return str(row.get("method") or service.get("name") or "default")


def _slug(text: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80].rstrip("-")
        or "default"
    )


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _plot_conditions(bench: dict[str, Any]) -> dict[str, Any]:
    """Expose fixed lengths and numeric SLO targets as axes without changing run conditions."""
    conditions = dict(bench)
    for minimum, maximum, axis in (
        ("min_prompt_length", "max_prompt_length", "prompt_length"),
        ("min_output_length", "max_output_length", "output_length"),
    ):
        length = _numeric(conditions.get(minimum))
        if length is not None and length == _numeric(conditions.get(maximum)):
            conditions.pop(minimum)
            conditions.pop(maximum)
            conditions[axis] = length
    groups = conditions.get("slo_params")
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        return conditions
    from benchmarks.integrations.evalscope.slo import parse_slo_criteria

    rules = parse_slo_criteria(groups)[0]
    if any(str(rule) in {"max", "min"} for rule in rules.values()):
        return conditions
    conditions.pop("slo_params")
    for metric, rule in rules.items():
        axis = f"slo_{metric}"
        conditions[axis] = _numeric(rule.target)
        conditions[f"{axis}_comparison"] = str(rule).split(maxsplit=1)[0]
    return conditions


def sweep_charts(
    points: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    *,
    metrics: tuple[str, ...] = (),
    methods: tuple[str, ...] = (),
) -> tuple[Chart, ...]:
    """Return metric curves for each parameter group and fixed-workload slice.

    The summary owns means, sample counts and run-level standard deviations.
    Each changing numeric bench parameter becomes an x axis; all other bench
    parameters remain fixed within a chart, so unrelated workloads never form
    one connected curve. W&B and the local renderer consume these same charts.
    """
    point_by_key = {(_method(p), str(p["combination"])): p for p in points}
    method_order = list(dict.fromkeys(_method(point) for point in points))
    method_order.extend(
        name
        for name in dict.fromkeys(_method(row) for row in summary)
        if name not in method_order
    )
    unknown_methods = set(methods) - set(method_order)
    unknown_metrics = set(metrics) - {str(row["metric"]) for row in summary}
    if unknown_methods or unknown_metrics:
        raise ValueError(
            "Unknown plot selection: "
            + ", ".join(sorted(unknown_methods | unknown_metrics))
        )
    selected = set(methods) if methods else None
    default_metrics = {
        "latency_p95_seconds",
        "ttft_p95_seconds",
        "tpot_p95_seconds",
        "requests_per_second",
        "generation_tokens_per_second",
        "e2e_s",
        "denoise_s",
        "peak_gpu_memory_mb",
        "slo_slo_attainment",
        "slo_request_goodput",
        "slo_token_goodput",
        "speculative_decoding_acceptance_ratio",
        "speculative_decoding_accepted_tokens_per_draft",
        "speculative_decoding_draft_mean_seconds",
        "speculative_decoding_target_forward_mean_seconds",
        "speculative_decoding_draft_time_share_ratio",
        "speculative_decoding_target_forward_time_share_ratio",
    }
    default_metrics.update(
        str(row["metric"])
        for row in summary
        if str(row["metric"]).startswith("gpu_allocation_gpu_hours_")
    )
    rows_by_group: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(
        list
    )
    for row in summary:
        metric = str(row["metric"])
        if metric not in (set(metrics) if metrics else default_metrics):
            continue
        point = point_by_key.get((_method(row), str(row["combination"])))
        method = _method(row, point)
        if selected is not None and method not in selected:
            continue
        bench = row.get("bench") or (point or {}).get("bench") or {}
        group = str(
            row.get("parameter_group")
            or (point or {}).get("parameter_group")
            or "default"
        )
        rows_by_group[group].append(
            (
                row,
                _plot_conditions({
                    k: v
                    for k, v in bench.items()
                    if k != "service" and not k.startswith("_")
                }),
            )
        )

    charts: list[Chart] = []
    chart_names: dict[str, int] = defaultdict(int)
    for group, entries in rows_by_group.items():
        varying = {
            key
            for key in dict.fromkeys(k for _, bench in entries for k in bench)
            if len({json.dumps(bench.get(key), sort_keys=True) for _, bench in entries})
            > 1
        }
        axes = [
            key
            for key in dict.fromkeys(k for _, bench in entries for k in bench)
            if len({_numeric(bench.get(key)) for _, bench in entries}) > 1
            and all(_numeric(bench.get(key)) is not None for _, bench in entries)
        ]
        if not axes:
            axes = [""]
        for axis in axes:
            slices: dict[
                tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
            ] = defaultdict(list)
            for row, bench in entries:
                # A fixed setting (including a categorical one) belongs in the facet,
                # not in the line joining points on the selected numeric axis.
                fixed = json.dumps(
                    {key: value for key, value in bench.items() if key != axis},
                    sort_keys=True,
                )
                slices[(str(row["metric"]), fixed)].append((row, bench))
            for (metric, fixed), slice_rows in slices.items():
                milliseconds = (
                    metric.startswith(("tpot_", "itl_")) and metric.endswith("_seconds")
                    or metric in {"speculative_decoding_draft_mean_seconds", "speculative_decoding_target_forward_mean_seconds"}
                )
                ratio = metric in {
                    "slo_slo_attainment", "speculative_decoding_acceptance_ratio",
                    "speculative_decoding_draft_time_share_ratio",
                    "speculative_decoding_target_forward_time_share_ratio",
                }
                scale = 1000 if milliseconds else 100 if ratio else 1
                by_method: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(
                    list
                )
                for row, bench in slice_rows:
                    x = _numeric(bench[axis]) if axis else 0.0
                    if x is not None:
                        by_method[_method(row)].append((x, row))
                series = tuple(
                    Series(
                        name=method,
                        x=tuple(x for x, _ in ordered),
                        y=tuple(
                            float(row["mean"]) * scale
                            if row["mean"] is not None
                            else math.nan
                            for _, row in ordered
                        ),
                        stddev=tuple(
                            error * scale
                            if (error := _numeric(row.get("stddev"))) is not None
                            else None
                            for _, row in ordered
                        ),
                        records=tuple(row for _, row in ordered),
                    )
                    for method in method_order
                    if (
                        ordered := sorted(
                            by_method.get(method, []), key=lambda pair: pair[0]
                        )
                    )
                )
                if not series:
                    continue
                shown = ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(slice_rows[0][1].items())
                    if key in varying and key != axis
                )
                title = group + (f" · {shown}" if shown else "")
                if metric.startswith("gpu_allocation_gpu_hours_"):
                    title += f" · {metric.removeprefix('gpu_allocation_gpu_hours_')}"
                base = _slug(
                    f"sweep-{group}-{metric}-{axis or 'configuration'}-{shown}"
                )
                panels: dict[int, list[Series]] = defaultdict(list)
                for item in series:
                    panels[method_order.index(item.name) // 8].append(item)
                for panel, panel_series in panels.items():
                    if not any(
                        math.isfinite(y) for item in panel_series for y in item.y
                    ):
                        continue
                    name = base + (
                        f"-panel-{panel + 1}" if len(method_order) > 8 else ""
                    )
                    chart_names[name] += 1
                    if chart_names[name] > 1:
                        name += f"-{chart_names[name]}"
                    if axis.startswith("slo_"):
                        unit = {"slo_rps": "req/s", "slo_tps": "tokens/s"}.get(axis, "s")
                        xlabel = f"{axis.removeprefix('slo_').replace('_', ' ').upper()} threshold ({unit})"
                    else:
                        conversation_rate = axis == "request_rate" and any(
                            point_by_key.get((_method(row), str(row["combination"])), {}).get("multi_turn")
                            for row, _ in slice_rows
                        )
                        xlabel = {
                            "max_concurrency": "Concurrency limit",
                            "request_rate": (
                                "Conversation arrival rate (conv/s)" if conversation_rate
                                else "Target request rate (req/s)"
                            ),
                            "duration": "Duration (s)",
                            "prompt_length": "Input length (tokens)",
                            "output_length": "Output length (tokens)",
                        }.get(axis, axis.replace("_", " ")) if axis else "Configuration"
                    charts.append(
                        Chart(
                            name=name,
                            title=title,
                            xlabel=xlabel,
                            ylabel=(
                                {
                                    "speculative_decoding_draft_mean_seconds": "Draft GPU time per step (ms)",
                                    "speculative_decoding_target_forward_mean_seconds": "Target forward GPU time per step (ms)",
                                }.get(metric, metric.removesuffix("_seconds").replace("_", " ").upper() + " (ms)")
                            )
                            if milliseconds
                            else {
                                "latency_p95_seconds": "E2EL p95 (s)",
                                "ttft_p95_seconds": "TTFT p95 (s)",
                                "requests_per_second": "Request throughput (req/s)",
                                "generation_tokens_per_second": "Output throughput (tokens/s)",
                                "e2e_s": "Video E2E latency (s)",
                                "denoise_s": "Denoise latency (s)",
                                "peak_gpu_memory_mb": "Peak GPU memory (MiB)",
                                "slo_slo_attainment": "SLO attainment (%)",
                                "slo_request_goodput": "Request goodput (req/s)",
                                "slo_token_goodput": "Output goodput (tokens/s)",
                                "speculative_decoding_acceptance_ratio": "Accepted draft tokens (%)",
                                "speculative_decoding_accepted_tokens_per_draft": "Accepted tokens per draft",
                                "speculative_decoding_draft_time_share_ratio": "Draft share of measured GPU time (%)",
                                "speculative_decoding_target_forward_time_share_ratio": "Target forward share of measured GPU time (%)",
                            }.get(
                                metric,
                                "GPU-hours" if metric.startswith("gpu_allocation_gpu_hours_")
                                else "GPU-seconds" if metric.startswith(("gpu_allocation_gpu_seconds_", "gpu_allocation_observed_gpu_seconds_"))
                                else metric.replace("_", " "),
                            ),
                            series=tuple(panel_series),
                            tick_labels=(group,) if not axis else (),
                            metric=metric,
                        )
                    )
    return tuple(charts)
