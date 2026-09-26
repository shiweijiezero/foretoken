# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run one generated OpenAI-compatible HTTP workload."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.integrations.evalscope.performance import run_evalscope_standard_load
from benchmarks.model_service import ModelService
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    resolved_load_record,
)


class GeneratedLoadBenchmark:
    """Execute one generated load point through EvalScope and publish it through the result sinks."""

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

    def run(self, *, phase_label: str = "Measurement") -> BenchmarkRun:
        """Run the labeled workload phase and return its published result."""
        load_record = resolved_load_record(self.benchmark)
        record = build_benchmark_run_record(
            self.benchmark, self.service, "standard_load", load_record
        )
        with ResultOutputs(
            self.benchmark,
            self.service,
            label=self.label,
            output_dir=self.output_dir,
            wandb_group=self.wandb_group,
        ) as outputs:
            outputs.open(record)
            profile = outputs.create_profile()
            with (profile if profile is not None else nullcontext()):
                if profile is not None:
                    profile.start_sync()
                metrics, measurements, time_origin = run_evalscope_standard_load(
                    self.benchmark,
                    self.service,
                    outputs.execution_dir,
                    phase_label=phase_label,
                    profile=profile,
                )
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts=(
                    {"profile": Path(outputs.execution_dir) / "profile.json"}
                    if profile is not None else {}
                ),
                time_origin=time_origin,
            )
            outputs.publish(run)
        return run
