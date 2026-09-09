# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run one standard OpenAI-compatible HTTP workload."""

from __future__ import annotations

from typing import Any, Optional

from benchmarks.config import HttpBenchmarkConfig
from benchmarks.deployment import BenchmarkRuntimeEndpoint
from benchmarks.request_execution.evalscope import run_evalscope_standard_load
from benchmarks.results.publication import (
    ResultPublication,
    build_benchmark_run_record,
    resolved_load_record,
)


class StandardHttpLoadBenchmark:
    """Execute and summarize one standard HTTP workload through the shared result publisher."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
        collect_request_measurements: bool = False,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self.collect_request_measurements = collect_request_measurements

    def run(self) -> dict[str, Any]:
        """Run one standard HTTP workload and return the existing result dictionary."""
        load_record = resolved_load_record(self.benchmark)
        run_record = build_benchmark_run_record(
            self.benchmark, self.endpoint, "standard_load", load_record
        )
        publication = ResultPublication(
            self.benchmark,
            self.endpoint,
            run_record,
            label=self.label,
            output_dir=self.output_dir,
            wandb_group=self.wandb_group,
        )
        with publication:
            metrics, request_measurements = run_evalscope_standard_load(
                self.benchmark,
                self.endpoint,
                publication.execution_dir,
                collect_request_measurements=self.collect_request_measurements,
            )
            publication.publish(request_measurements, metrics)

        return {
            "mode": "standard_load",
            "metrics": metrics,
            "raw": request_measurements,
            "output_dir": publication.output_dir,
        }
