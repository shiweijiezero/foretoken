# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch one benchmark measurement without composing sweeps or SLO searches."""

from __future__ import annotations

from typing import TypeAlias

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.output import BenchmarkRun
from benchmarks.runs.executor import TaskLoadBenchmark, load_multi_dataset_tasks
from benchmarks.runs.http import GeneratedLoadBenchmark
from benchmarks.runs.trace import TraceReplayBenchmark

MeasurementBenchmark: TypeAlias = (
    TaskLoadBenchmark | GeneratedLoadBenchmark | TraceReplayBenchmark
)


def measurement_runner(
    benchmark: BenchmarkConfig,
    service: ModelService,
    *,
    label: str = "",
    output_dir: str | None = None,
    wandb_group: str | None = None,
) -> MeasurementBenchmark:
    """Select the executor for one workload measurement point."""
    if benchmark.trace.trace_selector:
        return TraceReplayBenchmark(
            benchmark, service, label=label, output_dir=output_dir, wandb_group=wandb_group
        )
    workload = benchmark.resolved_workload
    if workload.has_multiple_datasets:
        grouped = load_multi_dataset_tasks(benchmark)
        return TaskLoadBenchmark(
            benchmark,
            service,
            tasks=grouped.pop("__global__"),
            dataset_tasks=grouped,
            label=label,
            output_dir=output_dir,
            wandb_group=wandb_group,
        )
    if (
        benchmark.is_multi_turn
        or benchmark.load.arrival_pattern != "poisson"
        or benchmark.load.duration_seconds is not None
        or benchmark.load.warmup_requests
    ):
        return TaskLoadBenchmark(
            benchmark, service, label=label, output_dir=output_dir, wandb_group=wandb_group
        )
    return GeneratedLoadBenchmark(
        benchmark, service, label=label, output_dir=output_dir, wandb_group=wandb_group
    )


def run_benchmark_point(
    benchmark: BenchmarkConfig,
    service: ModelService,
    *,
    label: str = "",
    output_dir: str | None = None,
    wandb_group: str | None = None,
) -> BenchmarkRun:
    """Execute one workload measurement and return its published result."""
    return measurement_runner(
        benchmark,
        service,
        label=label,
        output_dir=output_dir,
        wandb_group=wandb_group,
    ).run()
