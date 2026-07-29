# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Select single vs multi runner."""

from __future__ import annotations

from benchmarks.config import BenchConfig
from benchmarks.runner.base import Runner
from benchmarks.runner.single import SingleRunner


def select_runner(config: BenchConfig) -> Runner:
    """Choose runner from config.

    - ``load.is_sweep`` → multi (not implemented in this phase)
    - else → single-point load test
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
    return SingleRunner(config)
