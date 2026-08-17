# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Select which runner to use for a benchmark."""

from __future__ import annotations

from benchmarks.config import BenchConfig
from benchmarks.runner.base import Runner
from benchmarks.runner.load_sweep import LoadSweepRunner
from benchmarks.runner.multi_dataset import MultiDatasetRunner
from benchmarks.runner.param_sweep import ParamSweepRunner
from benchmarks.runner.run_benchmark import RunBenchmark


def select_runner(config: BenchConfig) -> Runner:
    """Choose runner from config.

    - ``param_sweep.enabled`` selects ``ParamSweepRunner``
    - ``load.is_sweep`` selects ``LoadSweepRunner``
    - multi-value ``--dataset`` selects ``MultiDatasetRunner``
    - otherwise selects ``RunBenchmark`` for a single-point load test
    """
    config.validate()
    if config.param_sweep.enabled:
        return ParamSweepRunner(config)
    if config.load.is_sweep:
        return LoadSweepRunner(config)
    if config.dataset.is_multi:
        return MultiDatasetRunner(config)
    return RunBenchmark(config)
