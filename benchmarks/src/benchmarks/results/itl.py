# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Incrementally summarize chunk intervals without retaining per-chunk timing."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import TypedDict

from ddsketch import DDSketch

_RELATIVE_ERROR = 0.01


class ITLSummary(TypedDict):
    """Lightweight timing record in seconds; only quantiles are approximate."""

    count: int
    mean: float | None
    max: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    relative_error: float | None


class ITLStatistics:
    """Own one live request or aggregate distribution with 1% relative quantile error.

    The dense, non-collapsing DDSketch grows with the observed value range, not
    chunk count. Runners merge successful requests at completion and retain only
    their summaries. Original values supply count, mean, and the SLO maximum.
    """

    def __init__(self) -> None:
        self._sketch = DDSketch(relative_accuracy=_RELATIVE_ERROR)
        self._maximum: float | None = None

    def observe(self, seconds: float) -> None:
        """Accumulate one interval from the streaming client's monotonic clock."""
        self._sketch.add(seconds)
        self._maximum = seconds if self._maximum is None else max(self._maximum, seconds)

    def merge(self, other: ITLStatistics) -> None:
        """Copy a completed request's distribution into its owning run or group."""
        self._sketch.merge(other._sketch)
        if other._maximum is not None:
            self._maximum = (
                other._maximum if self._maximum is None
                else max(self._maximum, other._maximum)
            )

    def summary(self) -> ITLSummary:
        """Publish scalar seconds and nearest-rank quantiles, never sketch bins."""
        count = int(self._sketch.count)
        quantiles: dict[str, float | None] = {}
        for name, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
            # DDSketch uses q*(n-1); select the benchmark's nearest rank.
            # Round upward so division cannot put an integer rank below its bin.
            rank = math.ceil(fraction * count) - 1
            quantile = rank / (count - 1) if count > 1 else 0.0
            quantiles[name] = self._sketch.get_quantile_value(
                min(math.nextafter(quantile, math.inf), 1.0)
            ) if count else None
        return {
            "count": count,
            "mean": self._sketch.avg if count else None,
            "max": self._maximum,
            "p50": quantiles["p50"],
            "p95": quantiles["p95"],
            "p99": quantiles["p99"],
            "relative_error": _RELATIVE_ERROR,
        }


def combine_itl_summaries(summaries: Iterable[ITLSummary]) -> ITLSummary:
    """Recover count/mean/max from records when no aggregate sketch exists.

    Per-request percentiles cannot recover cross-request percentiles; these stay
    unavailable for consumers that only have lightweight saved request records.
    """
    count = 0
    total = 0.0
    maximum: float | None = None
    for summary in summaries:
        if summary["count"]:
            count += summary["count"]
            total += summary["mean"] * summary["count"]
            maximum = summary["max"] if maximum is None else max(maximum, summary["max"])
    return {
        "count": count,
        "mean": total / count if count else None,
        "max": maximum,
        "p50": None,
        "p95": None,
        "p99": None,
        "relative_error": None,
    }
