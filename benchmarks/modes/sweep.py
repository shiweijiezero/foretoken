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
"""List-sweep benchmark mode with Pareto frontier analysis."""

from __future__ import annotations

from typing import Any

from benchmarks.analyzer.pareto import (
    attach_pareto_metrics,
    pareto_frontier,
)
from benchmarks.analyzer.vllm_pareto import vllm_pareto_available
from benchmarks.client.openai_client import OpenAICompatClient
from benchmarks.metrics.aggregator import attach_user_throughput
from benchmarks.metrics.engine_collector import engine_timeseries_csv
from benchmarks.modes.base import BenchmarkMode
from benchmarks.report.summary import (
    print_pareto_frontier,
    print_summary,
    print_sweep_results,
)
from benchmarks.report.table import build_metrics_table
from benchmarks.runner.sweep_runner import SweepRunner
from benchmarks.storage.csv_writer import sweep_csv
from benchmarks.storage.wandb_writer import (
    default_wandb_group_name,
    sweep_point_run_name,
)
from benchmarks.sweep.pareto_plot import plot_pareto


class SweepMode(BenchmarkMode):
    """Run ``(parallel, number, rate)`` points, then Pareto + best summary."""

    async def run(self) -> dict[str, Any]:
        args = self.args
        writer = self.writer
        points = args.sweep_points()

        config = self._build_config(points)
        writer.save_json("config.json", config)

        wandb_group = self._wandb_group()
        point_wandb: dict[str, Any] = {"writer": None}
        aggregator = self.aggregator

        sweep = SweepRunner(
            OpenAICompatClient,
            args.url,
            args.model,
            points=points,
            api_key=args.api_key,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            stream=args.stream,
            requests=self.requests,
            open_loop=args.open_loop,
        )

        results = await sweep.run(
            wrap_run=self.engine.run,
            on_result_factory=self._on_result_factory(
                point_wandb, aggregator
            ),
            on_point_start=self._on_point_start(
                config, wandb_group, point_wandb
            ),
            on_point_end=self._on_point_end(point_wandb),
        )

        self._save_engine_csvs(results)
        attach_pareto_metrics(results, gpu_count=args.gpu_count)
        print_sweep_results(results)

        frontier = pareto_frontier(results, gpu_count=args.gpu_count)
        summary = self._build_summary(
            config, results, frontier, wandb_group
        )
        best = summary["best_throughput"]
        table = build_metrics_table(
            client_metrics=best,
            engine_summary=best.get("engine"),
        )
        self._persist(results, summary, table)
        self._plot_and_report(results, frontier, config, best)

        print(f"Results saved: {writer.output_dir}")
        return {
            "mode": "sweep",
            "summary": summary,
            "metrics_table": table,
            "output_dir": writer.output_dir,
        }

    def _build_config(
        self, points: list[tuple[int, int, float]]
    ) -> dict[str, Any]:
        args = self.args
        config = self.base_config("sweep")
        config["schema"] = "foretoken.list_sweep.v1"
        config["sweep_points"] = [
            {
                "parallel": (-1 if args.open_loop else p),
                "number": n,
                "rate": r,
            }
            for p, n, r in points
        ]
        config["gpu_count"] = args.gpu_count
        return config

    def _wandb_group(self) -> str:
        if not self.args.wandb:
            return ""
        group = default_wandb_group_name(
            model=self.args.model,
            run_name=self.args.wandb_run_name,
        )
        print(f"[wandb] list-sweep group: {group}")
        return group

    def _on_point_start(
        self,
        config: dict[str, Any],
        wandb_group: str,
        point_wandb: dict[str, Any],
    ):
        args = self.args
        writer = self.writer
        factory = self.wandb_factory

        def _hook(parallel: int, number: int, rate: float) -> None:
            if not args.wandb:
                return
            log_parallel = -1 if args.open_loop else parallel
            point_config = dict(config)
            point_config["resolved"] = {
                "parallel": log_parallel,
                "number": number,
                "rate": rate,
            }
            point_wandb["writer"] = factory.create(
                point_config,
                wandb_dir=writer.output_dir,
                run_name=sweep_point_run_name(
                    wandb_group,
                    parallel=log_parallel,
                    number=number,
                    rate=rate,
                    open_loop=args.open_loop,
                ),
                group=wandb_group,
                job_type="list-sweep",
            )

        return _hook

    def _on_result_factory(
        self,
        point_wandb: dict[str, Any],
        aggregator,
    ):
        args = self.args

        def _factory(parallel: int, _number: int, rate: float):
            wb = point_wandb.get("writer")
            if wb is None or not wb.active:
                return None
            log_parallel = -1 if args.open_loop else parallel

            def _on_result(partial: dict[str, Any]) -> None:
                active = point_wandb.get("writer")
                if active is None or not active.active:
                    return
                partial_metrics = aggregator.aggregate(partial)
                attach_user_throughput(
                    partial_metrics, parallel=log_parallel
                )
                active.log_perf(
                    partial_metrics,
                    parallel=log_parallel,
                    request_rate=rate,
                )

            return _on_result

        return _factory

    def _on_point_end(self, point_wandb: dict[str, Any]):
        def _hook(metrics: dict[str, Any]) -> None:
            wb = point_wandb.get("writer")
            if wb is None:
                return
            if wb.active:
                wb.log_perf(
                    metrics,
                    parallel=metrics.get("parallel"),
                    request_rate=float(metrics.get("rate", -1)),
                )
                wb.finish()
            point_wandb["writer"] = None

        return _hook

    def _save_engine_csvs(self, results: list[dict[str, Any]]) -> None:
        writer = self.writer
        for metrics in results:
            timeseries = metrics.pop("_engine_timeseries", None)
            if not timeseries:
                continue
            csv_text = engine_timeseries_csv(timeseries)
            if csv_text:
                writer.save_artifact(
                    f"engine_metrics_p{metrics['parallel']}.csv",
                    csv_text,
                )

    def _build_summary(
        self,
        config: dict[str, Any],
        results: list[dict[str, Any]],
        frontier: list[dict[str, Any]],
        wandb_group: str,
    ) -> dict[str, Any]:
        return {
            "schema": "foretoken.list_sweep.v1",
            "mode": "sweep",
            "gpu_count": self.args.gpu_count,
            "config": config,
            "wandb_group": wandb_group or None,
            "best_throughput": max(
                results,
                key=lambda x: x["throughput"]["token/s"],
            ),
            "pareto_frontier": frontier,
            "results": results,
        }

    def _persist(
        self,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        table: dict[str, Any],
    ) -> None:
        writer = self.writer
        # Aggregated per-point metrics; do not use raw.json.
        writer.save_json("sweep_points.json", results)
        writer.save_json("summary.json", summary)
        writer.save_json("metrics_table.json", table)
        writer.save_artifact("sweep.csv", sweep_csv(results))

    def _plot_and_report(
        self,
        results: list[dict[str, Any]],
        frontier: list[dict[str, Any]],
        config: dict[str, Any],
        best: dict[str, Any],
    ) -> None:
        writer = self.writer
        plot_path = f"{writer.artifact_dir}/pareto.png"
        plot_pareto(
            results,
            plot_path,
            frontier=frontier,
            gpu_count=self.args.gpu_count,
        )
        backend = "vllm" if vllm_pareto_available() else "foretoken"
        print(
            f"\n[pareto] backend={backend} "
            "axes=tokens/s/user vs tokens/s/GPU"
        )
        print_pareto_frontier(frontier)

        best_config = dict(config)
        best_config["resolved"] = {
            "parallel": best.get("parallel"),
            "number": best.get("number"),
            "rate": best.get("rate", -1),
        }
        print("\nBest throughput point")
        print_summary(best_config, best)
