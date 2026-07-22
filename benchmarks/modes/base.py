# Copyright 2026 Foretoken contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Abstract base for single / sweep benchmark modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from benchmarks.arguments import BenchArguments
from benchmarks.metrics.aggregator import MetricsAggregator
from benchmarks.modes.engine import EngineMetricsSession
from benchmarks.modes.wandb_factory import WandbFactory
from benchmarks.storage.result_writer import ResultWriter


class BenchmarkMode(ABC):
    """Shared setup for a benchmark run: writer, metrics, W&B, engine."""

    def __init__(
        self,
        args: BenchArguments,
        requests: Optional[list[dict]] = None,
    ):
        self.args = args
        self.requests = requests
        self.writer = ResultWriter(root_dir=args.outputs_dir)
        self.aggregator = MetricsAggregator()
        self.wandb_factory = WandbFactory(args)
        self.engine = EngineMetricsSession(args)

    @property
    def resolved_parallel(self) -> int:
        """Open-loop uses ``-1`` (EvalScope / metrics convention)."""
        return -1 if self.args.open_loop else self.args.primary_parallel

    def base_config(self, mode: str) -> dict[str, Any]:
        config = self.args.run_config()
        config["mode"] = mode
        config["resolved"] = {
            "parallel": self.resolved_parallel,
            "number": self.args.primary_number,
            "rate": self.args.primary_rate,
        }
        config["collect_engine_metrics"] = self.args.collect_engine_metrics
        return config

    @abstractmethod
    async def run(self) -> dict[str, Any]:
        """Execute the mode and return a result dict."""
