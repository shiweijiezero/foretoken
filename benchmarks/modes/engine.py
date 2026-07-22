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
"""Optional engine ``/metrics`` sampling around a benchmark coroutine."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from benchmarks.arguments import BenchArguments
from benchmarks.metrics.engine_collector import build_engine_collector

RunCoro = Callable[[], Awaitable[Any]]


class EngineMetricsSession:
    """Run a coroutine while optionally collecting engine Prometheus metrics."""

    def __init__(self, args: BenchArguments):
        self.args = args

    async def run(
        self, run_coro: RunCoro
    ) -> tuple[Any, Optional[dict[str, Any]]]:
        if not self.args.collect_engine_metrics:
            return await run_coro(), None

        args = self.args
        collector = build_engine_collector(
            args.url,
            metrics_url=args.engine_metrics_url,
            api_key=args.api_key,
            interval=args.engine_metrics_interval,
        )
        await collector.start()
        try:
            output = await run_coro()
        finally:
            engine = await collector.stop()
        return output, engine
