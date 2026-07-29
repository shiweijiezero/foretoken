# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Runner protocol shared by single / multi benchmarks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from benchmarks.config import BenchConfig


class Runner(ABC):
    """Benchmark runner: one ``async run()`` entry."""

    def __init__(self, config: BenchConfig):
        self.config = config

    @abstractmethod
    async def run(self) -> dict[str, Any]:
        """Execute the benchmark and return a result dict."""
