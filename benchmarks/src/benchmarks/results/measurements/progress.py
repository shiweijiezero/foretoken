# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish completed HTTP requests and stable measurement windows while a run executes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from benchmarks.results.measurements.requests import (
    RequestMeasurement,
    request_slo_results,
)
from benchmarks.results.measurements.timeseries import (
    ELAPSED_TIME,
    REQUEST_INDEX,
    CompletionWindows,
    CumulativeMetrics,
    request_series,
)


class HttpProgress:
    """Own live chart state on the scheduler clock, without retaining response content."""

    def __init__(self, publish: Callable[..., None], *, stream: bool, criteria: dict[str, str] | None) -> None:
        self.publish = publish
        self.stream = stream
        self.criteria = criteria
        self.metrics = CumulativeMetrics(stream=stream)
        self.windows = CompletionWindows(stream=stream)
        self.active: dict[str, tuple[int, float]] = {}
        self.request_count = 0
        self.origin = 0.0

    def start(self, origin: float) -> None:
        """Use the scheduler clock after preparation and warmup complete."""
        self.origin = origin

    def request_started(self, request_id: str, timestamp: float) -> None:
        """Retain send order and the earliest unfinished request for safe window publication."""
        self.request_count += 1
        self.active[request_id] = (self.request_count, timestamp - self.origin)

    def observe(self, item: RequestMeasurement) -> None:
        """Publish a completed request and accumulate its metrics for live windows."""
        index, _ = self.active.pop(item.request_id)
        slo = request_slo_results([item], self.criteria, 1.0)
        decisions = slo["request_slo_met"] if slo is not None else None
        self.windows.slo = decisions is not None
        self.metrics.observe(item)
        self.windows.observe(item, decisions[0] if decisions is not None else None)
        row = next(request_series([item], stream=self.stream, slo_met=decisions))
        row[REQUEST_INDEX] = index
        self.publish(row, axis=REQUEST_INDEX)

    def emit(self, elapsed: float, *, final: bool = False) -> None:
        """Publish cumulative metrics and windows whose requests have all completed.

        Streaming latency may end before usage arrives. Waiting for earlier active
        requests preserves the same completion buckets as the final saved results.
        """
        self.publish(self.metrics.row(elapsed), axis=ELAPSED_TIME)
        boundary = min((start for _, start in self.active.values()), default=elapsed)
        for row in self.windows.rows(boundary, final=final):
            self.publish(row, axis=ELAPSED_TIME)

    async def sample(self) -> None:
        """Update measured progress once per second until the HTTP runner drains or stops."""
        while True:
            await asyncio.sleep(1)
            if self.origin:
                self.emit(time.perf_counter() - self.origin)
