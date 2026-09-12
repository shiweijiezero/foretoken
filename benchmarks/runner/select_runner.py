# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Select which runner to use for a benchmark."""

from __future__ import annotations

from benchmarks.config import BenchConfig
from benchmarks.runner.base import Runner
from benchmarks.runner.multi_dataset import MultiDatasetRunner
from benchmarks.runner.run_benchmark import RunBenchmark
from benchmarks.runner.run_spec import RunSpec
from benchmarks.runner.sla import SLARunner
from benchmarks.runner.sweep import SweepRunner
from benchmarks.runner.trace_runner import TraceRunner

def select_runner(config: BenchConfig) -> Runner:
    """Choose the runner for this benchmark config (CLI top level only)."""
    config.validate()
    if config.sla.auto_tune:
        return SLARunner(config)
    if config.dataset.trace_path:
        return TraceRunner(config)
    if config.param_sweep.bench_params:
        return SweepRunner(config)
    if config.dataset.is_multi:
        return MultiDatasetRunner(config)
    return RunBenchmark(RunSpec(config=config))
