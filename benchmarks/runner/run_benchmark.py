# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run one benchmark point: load, dispatch, aggregate, persist, and log."""

from __future__ import annotations

from typing import Any

from benchmarks.runner.base import Runner
from benchmarks.runner.run_spec import RunSpec
from benchmarks.workload.loader import load_requests


class RunBenchmark(Runner):
    """Execute one resolved closed-loop or open-loop load point.

    Semantics:
    - Default closed-loop: semaphore = ``parallel``.
    - ``rate > 0``: Poisson absolute-time pacing.
    - ``open_loop``: fire on schedule without semaphore backpressure.

    Orchestration context (output directory, W&B group/label) comes from
    ``RunSpec``, not from user config fields. Owns the per-point client,
    child writer, and W&B logger lifecycles.
    """

    def __init__(self, spec: RunSpec):
        super().__init__(spec.config)
        self.spec = spec

    async def run(self) -> dict[str, Any]:
        load = self.default_load()
        requests = load_requests(self.config)
        writer = self.create_writer(self.spec.output_dir)
        run_config = self.build_run_config("run_benchmark", load)
        label = self.spec.label.strip() or None
        client = self.create_client(load["parallel"], load["number"])
        try:
            wandb_logger = self.create_wandb_logger(
                writer,
                load,
                name_suffix=label,
                group=self.spec.wandb_group,
            )
            try:
                raw_output = await self.dispatch(
                    client,
                    requests,
                    parallel=load["parallel"],
                    rate=load["rate"],
                    open_loop=load["open_loop"],
                )
                metrics = self.aggregate_metrics(
                    raw_output,
                    rate=load["rate"],
                    number=load["number"],
                    resolved_parallel=load["resolved_parallel"],
                )
                self.save_results(
                    writer,
                    run_config,
                    raw_output,
                    metrics,
                    wandb_logger=wandb_logger,
                )
            except Exception:
                wandb_logger.finish()
                raise
        finally:
            await client.close()

        return {
            "mode": "run_benchmark",
            "metrics": metrics,
            "raw": raw_output,
            "output_dir": writer.output_dir,
        }
