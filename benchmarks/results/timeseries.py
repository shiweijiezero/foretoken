# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build completion-window and send-order series from measured requests."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from benchmarks.results.metrics import RequestMeasurement, percentile_summary

ELAPSED_TIME = "Elapsed time (s)"
REQUEST_INDEX = "Request index (send order)"


def request_series(
    measurements: list[RequestMeasurement], *, stream: bool
) -> Iterator[dict[str, Any]]:
    """Yield each logical request in stable send order, including failures."""
    for index, item in enumerate(sorted(measurements, key=lambda item: item.started_at), 1):
        row = {
            REQUEST_INDEX: index,
            "Requests/E2EL (s)": item.latency,
            "Requests/Input tokens": item.input_tokens,
            "Requests/Output tokens": item.output_tokens,
            "Requests/Success": int(item.succeeded),
        }
        if stream and item.succeeded:
            if item.ttft is not None:
                row["Requests/TTFT (ms)"] = item.ttft * 1000
            if item.tpot is not None:
                row["Requests/TPOT (ms)"] = item.tpot * 1000
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
    events: list[tuple[float, int]] = []
    for item in measurements:
        stop = item.started_at + item.latency
        completed[min(int(stop), count - 1)].append(item)
        arrivals[min(int(item.started_at), count - 1)] += 1
        events.extend(((item.started_at, 1), (stop, -1)))
    events.sort()
    # Integrate concurrency between request starts and finishes, carrying active
    # requests across window boundaries rather than sampling only at each edge.
    event_index = active = attempted = succeeded = 0
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
        attempted += len(rows)
        succeeded += len(successful)
        row: dict[str, Any] = {
            ELAPSED_TIME: right,
            "Time/Window duration (s)": width,
            "Time/Started requests per second": arrivals[index] / width,
            "Time/Completed requests": attempted,
            "Time/Successful requests": succeeded,
            "Time/Failed requests": attempted - succeeded,
            "Time/Request throughput (req/s)": len(successful) / width,
            "Time/Completed input tokens per second": sum(item.input_tokens for item in successful) / width,
            "Time/Completed output tokens per second": sum(item.output_tokens for item in successful) / width,
            "Time/Mean in-flight requests": area / width,
        }
        if rows:
            row["Time/Failure rate (%)"] = 100 * (len(rows) - len(successful)) / len(rows)
        if successful:
            row["Time/E2EL p95 (s)"] = percentile_summary([item.latency for item in successful])["p95"]
            if stream:
                for field, label in (("ttft", "TTFT"), ("tpot", "TPOT")):
                    values = [getattr(item, field) * 1000 for item in successful if getattr(item, field) is not None]
                    if values:
                        row[f"Time/{label} p95 (ms)"] = percentile_summary(values)["p95"]
        yield row
