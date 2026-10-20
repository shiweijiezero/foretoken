# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build completion-window and send-order series from measured requests."""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from benchmarks.results.measurements.requests import (
    RequestMeasurement,
    percentile_summary,
)

ELAPSED_TIME = "Elapsed time (s)"
REQUEST_INDEX = "Request index (send order)"


def request_series(
    measurements: list[RequestMeasurement],
    *,
    stream: bool,
    slo_met: list[bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each logical request in stable send order, including failures."""
    ordered = sorted(
        enumerate(measurements), key=lambda item: item[1].started_at
    )
    for index, (measurement_index, item) in enumerate(ordered, 1):
        row = {
            REQUEST_INDEX: index,
            "Requests/E2EL (s)": item.latency,
            "Requests/Success": int(item.succeeded),
        }
        if slo_met is not None:
            row["Requests/SLO met"] = int(slo_met[measurement_index])
        if item.input_tokens is not None:
            row["Requests/Input tokens"] = item.input_tokens
        if item.target_output_tokens is not None:
            row["Requests/Target output tokens"] = item.target_output_tokens
        if item.output_tokens is not None:
            row["Requests/Output tokens"] = item.output_tokens
        if item.cached_input_tokens is not None:
            row["Requests/Cached input tokens"] = item.cached_input_tokens
        if stream and item.succeeded:
            if item.ttft is not None:
                row["Requests/TTFT (s)"] = item.ttft
            if item.tpot is not None:
                row["Requests/TPOT (ms)"] = item.tpot * 1000
        yield row


class CumulativeMetrics:
    """Accumulate completed request metrics for live and saved-result consumers."""

    def __init__(self, *, stream: bool) -> None:
        self.stream = stream
        self.completed = self.succeeded = 0
        self.input_tokens: int | None = 0
        self.output_tokens: int | None = 0
        self.latency = self.ttft = self.tpot = self.itl = 0.0
        self.ttft_count = self.tpot_count = self.itl_count = 0

    def observe(self, item: RequestMeasurement) -> None:
        """Add one completed logical HTTP request, including failures in request counts."""
        self.completed += 1
        if not item.succeeded:
            return
        self.succeeded += 1
        self.input_tokens = (
            self.input_tokens + item.input_tokens
            if self.input_tokens is not None and item.input_tokens is not None else None
        )
        self.output_tokens = (
            self.output_tokens + item.output_tokens
            if self.output_tokens is not None and item.output_tokens is not None else None
        )
        self.latency += item.latency
        if self.stream:
            if item.ttft is not None:
                self.ttft += item.ttft
                self.ttft_count += 1
            if item.tpot is not None:
                self.tpot += item.tpot
                self.tpot_count += 1
            if item.itl_summary["count"]:
                self.itl += item.itl_summary["mean"] * item.itl_summary["count"]
                self.itl_count += item.itl_summary["count"]

    def row(self, elapsed: float) -> dict[str, Any]:
        """Publish counts, running mean latencies, and throughput since measurement start."""
        row: dict[str, Any] = {
            ELAPSED_TIME: elapsed,
            "Cumulative/Completed requests": self.completed,
            "Cumulative/Successful requests": self.succeeded,
            "Cumulative/Failed requests": self.completed - self.succeeded,
        }
        if self.completed:
            row["Cumulative/Success rate (%)"] = 100 * self.succeeded / self.completed
        if self.succeeded:
            row["Cumulative/Mean E2EL (s)"] = self.latency / self.succeeded
            if self.input_tokens is not None:
                row["Cumulative/Mean input tokens"] = self.input_tokens / self.succeeded
            if self.output_tokens is not None:
                row["Cumulative/Mean output tokens"] = self.output_tokens / self.succeeded
        if elapsed > 0:
            row["Cumulative/Request throughput (req/s)"] = self.succeeded / elapsed
            if self.input_tokens is not None:
                row["Cumulative/Input token throughput (tokens/s)"] = self.input_tokens / elapsed
            if self.output_tokens is not None:
                row["Cumulative/Output token throughput (tokens/s)"] = self.output_tokens / elapsed
            if self.input_tokens is not None and self.output_tokens is not None:
                row["Cumulative/Total token throughput (tokens/s)"] = (self.input_tokens + self.output_tokens) / elapsed
        if self.ttft_count:
            row["Cumulative/Mean TTFT (s)"] = self.ttft / self.ttft_count
        if self.tpot_count:
            row["Cumulative/Mean TPOT (ms)"] = 1000 * self.tpot / self.tpot_count
        if self.itl_count:
            row["Cumulative/Mean ITL (ms)"] = 1000 * self.itl / self.itl_count
        return row


def cumulative_series(
    measurements: list[RequestMeasurement], *, stream: bool
) -> Iterator[dict[str, Any]]:
    """Yield running aggregates in completion order on the elapsed-time axis."""
    metrics = CumulativeMetrics(stream=stream)
    for item in sorted(measurements, key=lambda item: item.started_at + item.latency):
        metrics.observe(item)
        yield metrics.row(item.started_at + item.latency)


class CompletionWindows:
    """Accumulate completed requests and emit immutable one-second measurement windows.

    Live callers advance only past starts of still-running requests. This keeps
    delayed usage chunks from moving a completion into an already published window.
    """

    def __init__(self, *, stream: bool, slo: bool = False) -> None:
        self.stream = stream
        self.slo = slo
        self.completed: dict[int, list[tuple[RequestMeasurement, bool | None]]] = defaultdict(list)
        self.arrivals: dict[int, int] = defaultdict(int)
        self.events: list[tuple[float, int]] = []
        self.index = self.active = 0
        self.position = 0.0

    def observe(self, item: RequestMeasurement, met: bool | None = None) -> None:
        """Add one completed request and its actual start/end events before window emission."""
        self.completed[int(item.started_at + item.latency)].append((item, met))
        self.arrivals[int(item.started_at)] += 1
        if item.latency > 0:
            heapq.heappush(self.events, (item.started_at, 1))
            heapq.heappush(self.events, (item.started_at + item.latency, -1))

    def rows(self, end: float, *, final: bool = False) -> Iterator[dict[str, Any]]:
        """Drain completed windows up to a safe clock boundary, including the final partial window."""
        count = math.ceil(end) if final else math.floor(end)
        if final and end > 0 and end == int(end):
            self.completed[count - 1].extend(self.completed.pop(count, []))
            self.arrivals[count - 1] += self.arrivals.pop(count, 0)
        while self.index < count:
            right = min(float(self.index + 1), end)
            width = right - self.index
            area = 0.0
            while self.events and self.events[0][0] <= right:
                timestamp, change = heapq.heappop(self.events)
                area += self.active * (timestamp - self.position)
                self.position = timestamp
                self.active += change
            area += self.active * (right - self.position)
            self.position = right
            finished = self.completed.pop(self.index, [])
            arrivals = self.arrivals.pop(self.index, 0)
            self.index += 1
            yield completion_window(
                finished, elapsed=right, width=width, arrivals=arrivals,
                mean_active=area / width, stream=self.stream, slo=self.slo,
            )


def completion_window(
    finished: list[tuple[RequestMeasurement, bool | None]], *, elapsed: float,
    width: float, arrivals: int, mean_active: float, stream: bool, slo: bool,
) -> dict[str, Any]:
    """Project one completed-request window onto shared live and saved chart fields."""
    rows = [item for item, _ in finished]
    successful = [item for item in rows if item.succeeded]
    row: dict[str, Any] = {
        ELAPSED_TIME: elapsed,
        "Time/Window duration (s)": width,
        "Time/Started requests per second": arrivals / width,
        "Time/Completed requests in window": len(rows),
        "Time/Successful requests in window": len(successful),
        "Time/Failed requests in window": len(rows) - len(successful),
        "Time/Request throughput (req/s)": len(successful) / width,
        "Time/Mean in-flight requests": mean_active,
    }
    for field, label in (("input_tokens", "input"), ("output_tokens", "output")):
        if all(getattr(item, field) is not None for item in successful):
            row[f"Time/Completed {label} tokens per second"] = sum(
                getattr(item, field) for item in successful
            ) / width
    if rows:
        row["Time/Failure rate (%)"] = 100 * (len(rows) - len(successful)) / len(rows)
    if slo:
        passing = [item for item, met in finished if met]
        if rows:
            row["Time/SLO attainment (%)"] = 100 * len(passing) / len(rows)
        row["Time/SLO request goodput (req/s)"] = len(passing) / width
        if all(item.output_tokens is not None for item in passing):
            row["Time/SLO token goodput (tokens/s)"] = sum(item.output_tokens for item in passing) / width
    for field, label, scale, unit in (
        ("latency", "E2EL", 1.0, "s"), ("ttft", "TTFT", 1.0, "s"),
        ("tpot", "TPOT", 1000.0, "ms"),
    ):
        if field != "latency" and not stream:
            continue
        values = [getattr(item, field) * scale for item in successful if getattr(item, field) is not None]
        if values:
            stats = percentile_summary(values)
            for percentile in ("p50", "p95", "p99"):
                row[f"Time/{label} {percentile} ({unit})"] = stats[percentile]
    return row


def time_series(
    measurements: list[RequestMeasurement], *, duration: float, stream: bool,
    slo_met: list[bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield completion-window throughput, latency percentiles, and time-weighted concurrency."""
    if not measurements:
        return
    end = max(duration, max(item.started_at + item.latency for item in measurements))
    windows = CompletionWindows(stream=stream, slo=slo_met is not None)
    for index, item in enumerate(measurements):
        windows.observe(item, slo_met[index] if slo_met is not None else None)
    yield from windows.rows(end, final=True)
