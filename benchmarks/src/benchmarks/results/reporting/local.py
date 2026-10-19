# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Report persistent result directories without owning their storage lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from benchmarks.results.types import BenchmarkRun

logger = logging.getLogger(__name__)


class LocalDirectorySink:
    """Report the persistent result directory selected by ResultOutputs."""

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        logger.info("Results saved: %s", self.output_dir)

    def close(self, *, exit_code: int = 0) -> None:
        return None
