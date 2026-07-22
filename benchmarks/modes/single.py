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
"""Single-point benchmark mode (fixed parallel / number / rate)."""

from __future__ import annotations

from typing import Any, Optional

from benchmarks.client.openai_client import OpenAICompatClient
from benchmarks.metrics.aggregator import attach_user_throughput
from benchmarks.metrics.engine_collector import engine_timeseries_csv
from benchmarks.modes.base import BenchmarkMode
from benchmarks.report.summary import print_summary
from benchmarks.report.table import build_metrics_table
from benchmarks.runner.single_runner import SingleRunner


class SingleMode(BenchmarkMode):
    """Run one closed-loop or open-loop load point and persist results."""

    async def run(self) -> dict[str, Any]:
        args = self.args
        writer = self.writer
        resolved_parallel = self.resolved_parallel
        rate = args.primary_rate

        config = self.base_config("single")
        config["engine_metrics_url"] = (
            args.engine_metrics_url or (args.url and "derived")
        )

        wandb = self.wandb_factory.create(
            config, wandb_dir=writer.output_dir
        )
        aggregator = self.aggregator

        def _on_result(partial: dict[str, Any]) -> None:
            if not wandb.active:
                return
            partial_metrics = aggregator.aggregate(partial)
            attach_user_throughput(
                partial_metrics, parallel=resolved_parallel
            )
            wandb.log_perf(
                partial_metrics,
                parallel=resolved_parallel,
                request_rate=rate,
            )

        client = OpenAICompatClient(
            args.url,
            args.model,
            timeout=args.timeout,
            api_key=args.api_key,
        )
        runner = SingleRunner(
            client,
            self.requests,
            args.primary_parallel,
            number=args.primary_number,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            stream=args.stream,
            on_result=_on_result if wandb.active else None,
            rate=rate,
            open_loop=args.open_loop,
        )

        result, engine = await self.engine.run(runner.run)
        metrics = aggregator.aggregate(result)
        metrics["rate"] = rate
        metrics["number"] = args.primary_number
        attach_user_throughput(metrics, parallel=resolved_parallel)

        engine_summary = self._attach_engine(metrics, engine)

        print_summary(config, metrics)
        table = build_metrics_table(
            client_metrics=metrics,
            engine_summary=engine_summary,
        )
        self._persist(config, result, metrics, table)

        if wandb.active:
            wandb.log_perf(
                metrics,
                parallel=resolved_parallel,
                request_rate=rate,
            )
            wandb.finish()

        print(f"\nResults saved: {writer.output_dir}")
        return {
            "mode": "single",
            "metrics": metrics,
            "metrics_table": table,
            "output_dir": writer.output_dir,
        }

    def _attach_engine(
        self,
        metrics: dict[str, Any],
        engine: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if engine is None:
            return None
        engine_summary = engine.get("summary")
        metrics["engine"] = engine_summary
        csv_text = engine_timeseries_csv(engine.get("timeseries") or [])
        if csv_text:
            self.writer.save_artifact("engine_metrics.csv", csv_text)
        return engine_summary

    def _persist(
        self,
        config: dict[str, Any],
        result: dict[str, Any],
        metrics: dict[str, Any],
        table: dict[str, Any],
    ) -> None:
        writer = self.writer
        writer.save_json("config.json", config)
        writer.save_json("raw.json", result["results"])
        writer.save_json(
            "summary.json",
            {
                "schema": "foretoken.single_metrics.v1",
                "metrics": metrics,
            },
        )
        writer.save_json("metrics_table.json", table)
