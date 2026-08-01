# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run one benchmark: load, dispatch, aggregate, persist, and log."""

from __future__ import annotations

from typing import Any

from benchmarks.runner.base import Runner
from benchmarks.workload.loader import load_requests


class RunOneBenchmark(Runner):
    """Run one closed-loop or open-loop load point.

    Semantics (EvalScope-aligned):
    - Default closed-loop: semaphore = ``parallel``.
    - ``rate > 0``: Poisson absolute-time pacing.
    - ``open_loop``: fire on schedule without semaphore backpressure.
    """

    async def run(self) -> dict[str, Any]:
        load = self.primary_load()
        requests = load_requests(self.config)
        writer = self.make_writer()
        run_config = self.build_run_config("run_one_benchmark", load)

        raw = await self.dispatch(
            self.make_client(),
            requests,
            parallel=load["parallel"],
            rate=load["rate"],
            open_loop=load["open_loop"],
        )
        metrics = self.aggregate_metrics(
            raw,
            rate=load["rate"],
            number=load["number"],
            resolved_parallel=load["resolved_parallel"],
        )
        self.save_results(writer, run_config, raw, metrics)

        return {
            "mode": "run_one_benchmark",
            "metrics": metrics,
            "output_dir": writer.output_dir,
        }
