# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project saved request, resource, and quality measurements onto chart coordinates."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from benchmarks.results.measurements.timeseries import (
    ELAPSED_TIME,
    REQUEST_INDEX,
    request_series,
    time_series,
)
from benchmarks.results.plots.data import Chart, Series, _numeric
from benchmarks.results.recording.requests import (
    load_http_measurements,
    read_request_records,
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


def _http_charts(source: Path, metrics: dict[str, Any], *, warmup: bool = False) -> list[Chart]:
    """Plot each HTTP phase on its own elapsed clock and request index."""
    raw = read_request_records(source, "warmup_raw_output" if warmup else "raw_output")
    if not raw:
        return []
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
