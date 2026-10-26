# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Share measured runs and result-destination contracts without importing their lifecycles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from benchmarks.config.benchmark import BenchmarkOutputConfig, WandbRunConfig
from benchmarks.results.measurements.requests import RequestMeasurement


@dataclass
class BenchmarkRun:
    """Result of one benchmark: its run record, aggregated metrics, per-request measurements, and artifacts.

    ``measurements`` is ``None`` for native records or aggregated points.
    ``artifacts`` maps a name to a file produced by the run. ``time_origin``
    is the monotonic clock value corresponding to elapsed time zero for observers.
    """

    record: dict[str, Any]
    metrics: dict[str, Any]
    measurements: list[RequestMeasurement] | None
    artifacts: dict[str, Path]
    time_origin: float | None = None
    # Execution outcome is distinct from measured scores and aggregate request counts.
    exit_code: int | None = None
    http_series_published: bool = False


class ResultSink(Protocol):
    """One result destination opened before the run executes and closed after publication."""

    def open(self, record: dict[str, Any]) -> None: ...

    def publish(self, run: BenchmarkRun) -> None: ...

    def close(self, *, exit_code: int = 0) -> None: ...


class ResultConfiguration(Protocol):
    """Expose result destinations and serializable inputs shared by benchmark runners."""

    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig

    def to_dict(self) -> dict[str, Any]: ...


_ResultSinkFactory = Callable[[str], list[ResultSink]]
_WandbPublisher = Callable[[Any, BenchmarkRun], None]
