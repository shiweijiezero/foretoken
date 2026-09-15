# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run one generated OpenAI-compatible HTTP workload."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from foretoken.arguments import ProfileCommand
from foretoken.profiling import ProfileRun

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.integrations.evalscope import run_evalscope_standard_load
from benchmarks.model_service import ModelService
from benchmarks.profiling.capture import BenchmarkProfile
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

    def run(self) -> BenchmarkRun:
        """Run the load point and return its published result."""
        load_record = resolved_load_record(self.benchmark)
        record = build_benchmark_run_record(
            self.benchmark, self.service, "standard_load", load_record
        )
        with ResultOutputs(
            self.benchmark,
            self.service,
            record,
            label=self.label,
            output_dir=self.output_dir,
            wandb_group=self.wandb_group,
        ) as outputs:
            profile_options = self.benchmark.profile
            profile = None
            if profile_options is not None:
                command = ProfileCommand(
                    kustomize_path=self.benchmark.service.kustomize_path,
                    model=self.service.model,
                    profile_engine=profile_options.engine,
                    profile_duration=profile_options.duration,
                    timeout=self.benchmark.service.wait_timeout,
                )
                profile = BenchmarkProfile(
                    ProfileRun(command, deployment=self.service.deployment),
                    outputs.execution_dir,
                )
            with (profile if profile is not None else nullcontext()):
                metrics, measurements = run_evalscope_standard_load(
                    self.benchmark,
                    self.service,
                    outputs.execution_dir,
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
            )
            outputs.publish(run)
        return run


def run_http_dataset(
    benchmark: BenchmarkConfig,
    service: ModelService,
    label: str,
    output_dir: str,
    wandb_group: str | None,
) -> BenchmarkRun:
    """Run one dataset with the output location and group selected by its composition."""
    return GeneratedLoadBenchmark(
        benchmark, service, label=label, output_dir=output_dir, wandb_group=wandb_group
    ).run()
