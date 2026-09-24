# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build completion-window and send-order series from measured requests."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from benchmarks.results.metrics import (
    RequestMeasurement,
    percentile_summary,
    request_activity_events,
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


def cumulative_series(
    measurements: list[RequestMeasurement], *, stream: bool
) -> Iterator[dict[str, Any]]:
    """Yield running aggregates in request-completion order on the elapsed-time axis."""
    completed = succeeded = 0
    input_tokens = output_tokens = 0
    input_tokens_complete = output_tokens_complete = True
    latency_total = ttft_total = tpot_total = itl_total = 0.0
    ttft_count = tpot_count = itl_count = 0
    ordered = sorted(
        enumerate(measurements),
        key=lambda item: (item[1].started_at + item[1].latency, item[0]),
    )
    for _, item in ordered:
        completed += 1
        if item.succeeded:
            succeeded += 1
            if item.input_tokens is None:
                input_tokens_complete = False
            else:
                input_tokens += item.input_tokens
            if item.output_tokens is None:
                output_tokens_complete = False
            else:
                output_tokens += item.output_tokens
            latency_total += item.latency
            if stream:
                if item.ttft is not None:
                    ttft_total += item.ttft
                    ttft_count += 1
                if item.tpot is not None:
                    tpot_total += item.tpot
                    tpot_count += 1
                itl_total += sum(item.itl_samples)
                itl_count += len(item.itl_samples)

        elapsed = item.started_at + item.latency
        row: dict[str, Any] = {
            ELAPSED_TIME: elapsed,
            "Cumulative/Completed requests": completed,
            "Cumulative/Successful requests": succeeded,
            "Cumulative/Failed requests": completed - succeeded,
            "Cumulative/Success rate (%)": 100.0 * succeeded / completed,
        }
        if succeeded:
            if input_tokens_complete:
                row["Cumulative/Mean input tokens"] = input_tokens / succeeded
            if output_tokens_complete:
                row["Cumulative/Mean output tokens"] = output_tokens / succeeded
            row["Cumulative/Mean E2EL (s)"] = latency_total / succeeded
        if elapsed > 0:
            row["Cumulative/Request throughput (req/s)"] = succeeded / elapsed
            if input_tokens_complete:
                row["Cumulative/Input token throughput (tokens/s)"] = (
                    input_tokens / elapsed
                )
            if output_tokens_complete:
                row["Cumulative/Output token throughput (tokens/s)"] = (
                    output_tokens / elapsed
                )
            if input_tokens_complete and output_tokens_complete:
                row["Cumulative/Total token throughput (tokens/s)"] = (
                    input_tokens + output_tokens
                ) / elapsed
        if ttft_count:
            row["Cumulative/Mean TTFT (s)"] = ttft_total / ttft_count
        if tpot_count:
            row["Cumulative/Mean TPOT (ms)"] = 1000.0 * tpot_total / tpot_count
        if itl_count:
            row["Cumulative/Mean ITL (ms)"] = 1000.0 * itl_total / itl_count
        yield row


def time_series(
    measurements: list[RequestMeasurement], *, duration: float, stream: bool
) -> Iterator[dict[str, Any]]:
    """Yield one-second completion windows and time-weighted in-flight request counts.

    Token throughput attributes a request's tokens to its completion window,
    not to individual streamed token arrivals. Empty completion windows report
    zero counts and throughput but have no latency or failure-rate sample.
    """
    if not measurements:
        return
    end = max(duration, max(item.started_at + item.latency for item in measurements))
    if end <= 0:
        return
    count = math.ceil(end)
    completed: dict[int, list[RequestMeasurement]] = defaultdict(list)
    arrivals: dict[int, int] = defaultdict(int)
    events = request_activity_events(measurements)
    for item in measurements:
        stop = item.started_at + item.latency
        completed[min(int(stop), count - 1)].append(item)
        arrivals[min(int(item.started_at), count - 1)] += 1
    # Integrate concurrency between request starts and finishes, carrying active
    # requests across window boundaries rather than sampling only at each edge.
    event_index = active = 0
    position = 0.0
    for index in range(count):
        left, right = float(index), min(float(index + 1), end)
        width = right - left
        area = 0.0
        while event_index < len(events) and events[event_index][0] <= right:
            timestamp, change = events[event_index]
            area += active * (timestamp - position)
            position = timestamp
            active += change
            event_index += 1
        area += active * (right - position)
        position = right
        rows = completed[index]
        successful = [item for item in rows if item.succeeded]
        row: dict[str, Any] = {
            ELAPSED_TIME: right,
            "Time/Window duration (s)": width,
            "Time/Started requests per second": arrivals[index] / width,
            "Time/Completed requests in window": len(rows),
            "Time/Successful requests in window": len(successful),
            "Time/Failed requests in window": len(rows) - len(successful),
            "Time/Request throughput (req/s)": len(successful) / width,
            "Time/Mean in-flight requests": area / width,
        }
        if all(item.input_tokens is not None for item in successful):
            row["Time/Completed input tokens per second"] = (
                sum(int(item.input_tokens) for item in successful) / width
            )
        if all(item.output_tokens is not None for item in successful):
            row["Time/Completed output tokens per second"] = (
                sum(int(item.output_tokens) for item in successful) / width
            )
        if rows:
            row["Time/Failure rate (%)"] = 100 * (len(rows) - len(successful)) / len(rows)
        if successful:
            row["Time/E2EL p95 (s)"] = percentile_summary(
                [item.latency for item in successful]
            )["p95"]
            if stream:
                timing_fields = (
                    ("ttft", "TTFT", 1.0, "s"),
                    ("tpot", "TPOT", 1000.0, "ms"),
                )
                for field, label, scale, unit in timing_fields:
                    values = [
                        getattr(item, field) * scale
                        for item in successful
                        if getattr(item, field) is not None
                    ]
                    if values:
                        row[f"Time/{label} p95 ({unit})"] = percentile_summary(values)["p95"]
        yield row
