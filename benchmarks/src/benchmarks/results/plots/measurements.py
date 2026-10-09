# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project saved request, resource, and quality measurements onto chart coordinates."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.results.metrics import RequestMeasurement
from benchmarks.results.plots.data import Chart, Series, _numeric
from benchmarks.results.replicas import gpu_allocation_history_rows
from benchmarks.results.timeseries import (
    ELAPSED_TIME,
    REQUEST_INDEX,
    request_series,
    time_series,
)


def _rows_chart(
    name: str, title: str, rows: list[dict[str, Any]], xkey: str, ykeys: tuple[str, ...]
) -> list[Chart]:
    """Project existing request/time-series values onto charts with one measurement unit."""
    charts = []
    for ykey in ykeys:
        selected = [
            (
                float(row[xkey]),
                float(row[ykey]) if _numeric(row.get(ykey)) is not None else math.nan,
                row,
            )
            for row in rows
            if _numeric(row.get(xkey)) is not None
        ]
        if not any(math.isfinite(y) for _, y, _ in selected):
            continue
        charts.append(
            Chart(
                name=f"{name}-{re.sub('[^a-z0-9]+', '-', ykey.lower()).strip('-')}",
                title=title + " · " + ykey.split("/")[-1],
                xlabel=xkey,
                ylabel=ykey.split("/")[-1],
                series=(
                    Series(
                        "Observed",
                        tuple(x for x, _, _ in selected),
                        tuple(y for _, y, _ in selected),
                        (None,) * len(selected),
                        tuple(row for _, _, row in selected),
                    ),
                ),
                metric=ykey,
            )
        )
    return charts


def load_http_measurements(raw: list[dict[str, Any]]) -> list[RequestMeasurement]:
    """Restore saved HTTP requests for plotting and phase-specific W&B curves."""
    return [
        RequestMeasurement(
            started_at=float(row["start_time"]),
            ttft=row.get("ttft"),
            latency=float(row["latency"]),
            tpot=row.get("tpot"),
            itl_samples=tuple(row.get("inter_token_latencies") or ()),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cached_input_tokens=row.get("cached_input_tokens"),
            succeeded=bool(row["success"]),
            conversation_id=row.get("conversation_id"),
            turn=row.get("turn"),
            status_code=row.get("status_code"),
            error_message=row.get("error"),
            dataset=row.get("dataset"),
            model=row.get("model"),
            priority=row.get("priority"),
            request_class=row.get("request_class"),
            target_output_tokens=row.get("target_output_tokens"),
        )
        for row in raw
    ]


def _http_charts(source: Path, metrics: dict[str, Any], *, warmup: bool = False) -> list[Chart]:
    """Plot each HTTP phase on its own elapsed clock and request index."""
    raw_path = source / ("warmup_raw_output.json" if warmup else "raw_output.json")
    if not raw_path.is_file():
        return []
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    measurements = load_http_measurements(raw)
    slo_met = (
        [bool(row["slo_met"]) for row in raw]
        if raw and all(row.get("slo_met") is not None for row in raw)
        else (metrics.get("slo") or {}).get("request_slo_met")
    )
    stream = bool(metrics["stream"])
    prefix = "warmup-" if warmup else ""
    phase = "Warmup · " if warmup else ""
    failed = sum(not item.succeeded for item in measurements)
    charts = []
    for field, label, factor in (
        ("latency", "E2EL (s)", 1),
        ("ttft", "TTFT (s)", 1),
        ("tpot", "TPOT (ms)", 1000),
    ):
        values = sorted(
            float(value) * factor
            for item in measurements
            if item.succeeded
            if (value := getattr(item, field)) is not None
        )
        if values:
            records = tuple(
                {
                    "metric": field,
                    "value": value,
                    "success_samples": len(values),
                    "failed_requests": failed,
                    "requested_requests": len(raw),
                }
                for value in values
            )
            charts.append(
                Chart(
                    f"{prefix}ecdf-{field}",
                    f"{phase}Empirical latency distribution · {label}",
                    label,
                    "Fraction of successful requests",
                    (
                        Series(
                            "Successful requests",
                            tuple(values),
                            tuple(i / len(values) for i in range(1, len(values) + 1)),
                            (None,) * len(values),
                            records,
                        ),
                    ),
                    kind="step",
                    metric=field,
                )
            )
    ordered = list(
        request_series(
            measurements,
            stream=stream,
            slo_met=slo_met,
        )
    )
    for row, original in zip(ordered, sorted(raw, key=lambda item: item["start_time"])):
        row.update(
            {
                "requested_requests": len(raw),
                "failed_requests": metrics["failed_num"],
                "status_code": original.get("status_code"),
                "error": original.get("error"),
            }
        )
    windows = list(
        time_series(
            measurements, duration=float(metrics["benchmark_time"]),
            stream=stream, slo_met=slo_met,
        )
    )
    charts += _rows_chart(
        f"{prefix}requests",
        f"{phase}Requests in send order",
        ordered,
        REQUEST_INDEX,
        (
            "Requests/E2EL (s)",
            "Requests/TTFT (s)",
            "Requests/TPOT (ms)",
            "Requests/Input tokens",
            "Requests/Output tokens",
            "Requests/Success",
            "Requests/SLO met",
        ),
    )
    charts += _rows_chart(
        f"{prefix}time",
        f"{phase}Per-second results",
        windows,
        ELAPSED_TIME,
        (
            "Time/Request throughput (req/s)",
            "Time/SLO attainment (%)",
            "Time/SLO request goodput (req/s)",
            "Time/SLO token goodput (tokens/s)",
            "Time/Completed output tokens per second",
            "Time/E2EL p95 (s)",
            "Time/TTFT p95 (s)",
            "Time/TPOT p95 (ms)",
            "Time/Failure rate (%)",
            "Time/Mean in-flight requests",
        ),
    )
    return charts


def phase_summary_charts(measured: dict[str, Any], warmup: dict[str, Any]) -> list[Chart]:
    """Compare phase summaries with the same aggregate statistics used by reports."""
    if "latency" in measured:
        fields = (("latency", "E2EL (s)"), ("ttft", "TTFT (s)"), ("tpot", "TPOT (s)"))
        values = [(name, measured[field].get("p95"), warmup.get(field, {}).get("p95")) for field, name in fields]
    else:
        fields = (("e2e_s", "E2E (s)"), ("queue_wait_s", "Queue wait (s)"), ("server_generation_s", "Server generation (s)"))
        values = [(name, measured.get(field), warmup.get(field)) for field, name in fields]
    rows = [item for item in values if item[1] is not None or item[2] is not None]
    if not rows:
        return []
    return [Chart(
        "phase-summary", "Warmup vs measurement", "Metric", "Value",
        (Series("Warmup", tuple(float(index) for index, _ in enumerate(rows)), tuple(float(item[2]) if item[2] is not None else math.nan for item in rows), (None,) * len(rows), tuple({"phase": "warmup", "metric": item[0]} for item in rows)),
         Series("Measurement", tuple(float(index) for index, _ in enumerate(rows)), tuple(float(item[1]) if item[1] is not None else math.nan for item in rows), (None,) * len(rows), tuple({"phase": "measurement", "metric": item[0]} for item in rows))),
        tick_labels=tuple(item[0] for item in rows), kind="scatter", metric="phase-summary",
    )]


def _video_phase_charts(source: Path, metrics: dict[str, Any]) -> list[Chart]:
    """Plot measured and warmup video timings as phase-labeled request series."""
    paths = [(source / "raw_results.json", "Measurement")]
    warmup = source / "warmup_raw_output.json"
    if warmup.is_file():
        paths.insert(0, (warmup, "Warmup"))
    charts: list[Chart] = []
    for field in ("e2e_s", "queue_wait_s", "server_generation_s"):
        series = []
        for path, label in paths:
            rows = json.loads(path.read_text(encoding="utf-8"))
            selected = [row for row in rows if row.get("success") and _numeric(row.get(field)) is not None]
            if selected:
                series.append(Series(label, tuple(float(row["index"]) for row in selected), tuple(float(row[field]) for row in selected), (None,) * len(selected), tuple(row for row in selected)))
        if series:
            charts.append(Chart(f"video-phase-{field}", f"Video phases · {field}", "Request index", f"{field} (s)", tuple(series), metric=field))
    return charts


def _prometheus_charts(source: Path) -> list[Chart]:
    """Plot observer series on their elapsed clock, retaining complete labels in CSV records."""
    path = source / "prometheus_observations.json"
    if not path.is_file():
        return []
    observations = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in observations["samples"]:
        elapsed = sample.get("elapsed_time_s")
        if elapsed is None:
            continue
        for name, query in sample["queries"].items():
            for result in query["result"]:
                labels = json.dumps(result.get("metric") or {}, sort_keys=True)
                value = result.get("value")
                numeric = (
                    _numeric(float(value[1]))
                    if value and value[1] is not None
                    else None
                )
                if numeric is not None:
                    grouped[(name, labels)].append(
                        {
                            "elapsed_time_s": elapsed,
                            "value": numeric,
                            "query": name,
                            "labels": labels,
                            "expression": query["expression"],
                            "observer_errors": observations["errors"],
                        }
                    )
    charts = []
    for name in dict.fromkeys(key[0] for key in grouped):
        def display_label(label: str, *, name: str = name) -> str:
            """Show service, group, role and rank without printing raw JSON in figure titles."""
            if not name.startswith("spec_"):
                return label
            values = json.loads(label)
            group = values.get("model_group") or values.get("modelservice") or values.get("model_name")
            role = values.get("model_role") or values.get("inference_foretoken_io_model_role")
            return " / ".join(str(item) for item in (group, role, values.get("engine")) if item is not None)

        series = tuple(
            Series(
                display_label(label),
                tuple(row["elapsed_time_s"] for row in grouped[(name, label)]),
                tuple(row["value"] for row in grouped[(name, label)]),
                (None,) * len(grouped[(name, label)]),
                tuple(grouped[(name, label)]),
            )
            for metric, label in grouped
            if metric == name
        )
        if series:
            unit = (
                "GPU s/s"
                if name in {"spec_draft_gpu_seconds_per_second", "spec_target_forward_gpu_seconds_per_second"}
                else "tokens/draft"
                if name == "spec_accepted_tokens_per_draft"
                else "ratio"
                if name.endswith("_ratio")
                else "W"
                if name.endswith("_watts")
                else "°C"
                if name.endswith("_celsius")
                else "tokens/s"
                if name.endswith("_per_second")
                else "selections/s"
                if name.endswith("_rate")
                else "requests"
            )
            title = {
                "spec_draft_gpu_seconds_per_second": "Draft GPU work",
                "spec_target_forward_gpu_seconds_per_second": "Target forward GPU work",
                "spec_draft_time_share_ratio": "Draft GPU-time share",
                "spec_target_forward_time_share_ratio": "Target forward GPU-time share",
                "spec_acceptance_ratio": "Draft acceptance",
                "spec_accepted_tokens_per_draft": "Accepted tokens per draft",
            }.get(name, f"Prometheus · {name.replace('_', ' ')}")
            charts.append(
                Chart(
                    f"prometheus-{name}",
                    title,
                    "Elapsed time (s)",
                    f"{title if name.startswith('spec_') else name.replace('_', ' ')} ({unit})",
                    series,
                    metric=name,
                )
            )
    return charts


def gpu_allocation_charts(source: Path) -> list[Chart]:
    """Plot sampled allocated GPU counts by resource on the benchmark clock.

    The observer owns coverage and integration. Saved history rows retain the
    boundary samples and unknown intervals, so the chart never estimates cost.
    """
    path = source / "gpu_allocation.json"
    if not path.is_file():
        return []
    allocation = json.loads(path.read_text(encoding="utf-8"))
    rows = gpu_allocation_history_rows(allocation)
    resources = sorted({key for row in rows for key in row if key.startswith("Resources/")})
    charts = []
    for field in resources:
        resource = field.removeprefix("Resources/").removesuffix("/Allocated GPUs")
        records = tuple({**row, "resource_name": resource, "duration_s": allocation["duration_s"]} for row in rows)
        values = tuple(
            float(row[field]) if _numeric(row.get(field)) is not None else math.nan
            for row in rows
        )
        if not any(math.isfinite(value) for value in values):
            continue
        charts.append(Chart(
            name=f"gpu-allocation-{re.sub('[^a-z0-9]+', '-', resource.lower()).strip('-')}",
            title=f"Allocated GPUs · {resource}",
            xlabel=ELAPSED_TIME,
            ylabel=f"Allocated GPUs ({resource})",
            series=(Series(
                "Observed", tuple(float(row[ELAPSED_TIME]) for row in rows),
                values, (None,) * len(rows), records,
            ),),
            kind="step",
            metric=f"gpu_allocation_{resource}",
        ))
    return charts


def _video_charts(source: Path, metrics: dict[str, Any]) -> list[Chart]:
    """Show successful video request timings in persisted request order."""
    path = source / "raw_results.json"
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    charts = []
    for field in (
        "e2e_s",
        "queue_wait_s",
        "server_generation_s",
        "preprocess_s",
        "encode_s",
        "denoise_s",
        "decode_s",
        "postprocess_s",
        "peak_gpu_memory_mb",
    ):
        selected = [
            row
            for row in rows
            if row["success"] and _numeric(row.get(field)) is not None
        ]
        if not selected:
            continue
        unit = "MiB" if field.endswith("_mb") else "s"
        charts.append(
            Chart(
                f"video-{field}",
                f"Video requests · {field}",
                "Request index",
                f"{field} ({unit})",
                (
                    Series(
                        "Successful requests",
                        tuple(float(row["index"]) for row in selected),
                        tuple(float(row[field]) for row in selected),
                        (None,) * len(selected),
                        tuple(
                            {
                                **row,
                                "success_samples": len(selected),
                                "failed_requests": metrics["request_num"]
                                - metrics["success_num"],
                            }
                            for row in selected
                        ),
                    ),
                ),
                metric=field,
            )
        )
    return charts


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


def _slo_charts(search: dict[str, Any]) -> list[Chart]:
    """Plot the search's saved probe averages, retaining each probe decision in tables."""
    charts = []
    for group in dict.fromkeys(probe["group"] for probe in search["probes"]):
        rows = sorted(
            (row for row in search["probes"] if row["group"] == group),
            key=lambda row: row["max_concurrency"],
        )
        fields = list(
            dict.fromkeys(field for row in rows for field in row["average_values"])
        )
        for field in fields + [
            "slo_attainment",
            "request_goodput",
            "token_goodput",
            "peak_request_concurrency",
        ]:
            chosen = [
                row
                for row in rows
                if _numeric(
                    row["average_values"].get(field)
                    if field in fields
                    else row.get(field)
                )
                is not None
            ]
            if chosen:
                charts.append(
                    Chart(
                        f"slo-group-{group}-{field.replace('_', '-')}",
                        f"SLO group {group} · {field}",
                        f"Configured concurrency ({search['concurrency_limit_unit']})",
                        field,
                        (
                            Series(
                                "Probe average",
                                tuple(float(row["max_concurrency"]) for row in chosen),
                                tuple(
                                    float(
                                        row["average_values"].get(field)
                                        if field in fields
                                        else row[field]
                                    )
                                    for row in chosen
                                ),
                                (None,) * len(chosen),
                                tuple(chosen),
                            ),
                        ),
                        metric=field,
                    )
                )
    return charts
