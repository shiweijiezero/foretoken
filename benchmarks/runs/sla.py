# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run SLA concurrency search and publish the satisfied load point."""

from __future__ import annotations

import os
from typing import Optional

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.integrations.evalscope import run_evalscope_sla_auto_tune
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_sla_results
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    result_directory_path,
    write_json,
)


class SlaAutoTuneBenchmark:
    """Search the maximum concurrency that satisfies SLA constraints."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group

    def run(self) -> BenchmarkRun:
        """Run the search and publish metrics for the satisfied concurrency."""
        execution_dir = result_directory_path(self.benchmark, self.output_dir)
        os.makedirs(execution_dir, exist_ok=True)
        metrics, measurements, sla_artifact, time_origin = run_evalscope_sla_auto_tune(
            self.benchmark,
            self.service,
            execution_dir,
        )
        load_record = {
            "parallel": int(metrics["parallel"]),
            "number": int(metrics["number"]),
            "rate": float(metrics["rate"]),
            "open_loop": False,
            "resolved_parallel": int(metrics["parallel"]),
        }
        record = build_benchmark_run_record(
            self.benchmark, self.service, "sla_auto_tune", load_record
        )
        with ResultOutputs(
            self.benchmark,
            self.service,
            record,
            label=self.label,
            output_dir=execution_dir,
            wandb_group=self.wandb_group,
        ) as outputs:
            sla_path = write_json(outputs.execution_dir, "sla_results.json", sla_artifact)
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts={"sla_results": sla_path},
                time_origin=time_origin,
            )
            outputs.publish(run)
            if not self.benchmark.outputs.includes("quiet"):
                log_sla_results(metrics["sla"])
        return run
