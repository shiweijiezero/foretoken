# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Select which runner to use for a benchmark."""

from __future__ import annotations

from benchmarks.config import BenchConfig
from benchmarks.runner.base import Runner
from benchmarks.runner.multi_dataset import MultiDatasetRunner
from benchmarks.runner.run_benchmark import RunBenchmark


def select_runner(config: BenchConfig) -> Runner:
    """Choose runner from config.

    - multi-value ``--dataset`` selects ``MultiDatasetRunner``
    - ``load.is_sweep`` / param sweep are not implemented in this phase
    - otherwise selects ``RunBenchmark`` for a single-point load test
    """
    config.validate()
    if config.param_sweep.enabled:
        raise NotImplementedError(
            "Parameter sweep is not implemented yet; omit "
            "--serve-params / --bench-params for a single run."
        )
    if config.load.is_sweep:
        raise NotImplementedError(
            "Load sweep (multi parallel/number/rate) is not implemented "
            "yet; pass a single --parallel / --number / --rate value."
        )
    if config.dataset.is_multi:
        return MultiDatasetRunner(config)
    return RunBenchmark(config)
