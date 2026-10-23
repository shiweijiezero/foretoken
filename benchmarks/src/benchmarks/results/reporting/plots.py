# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export saved figures after measurement artifact sinks finish publication."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from benchmarks.results.types import BenchmarkRun


class PlotSink:
    """Export figures from the same saved records used by independent redraws."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Render after artifact sinks finish and expose the figure directory to publishers."""
        from benchmarks.results.plots import render_results

        render_results(self.directory)
        run.artifacts["plots"] = self.directory / "plots"
        logger.info("Plots saved: {}", run.artifacts["plots"])

    def close(self, *, exit_code: int = 0) -> None:
        return None
