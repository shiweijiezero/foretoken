# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run one benchmark point: load, dispatch, aggregate, persist, and log."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from benchmarks.deployment.profiling import ProfileSession

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

    async def run_profile(self, session: ProfileSession, warmup_requests: int) -> dict[str, Any]:
        """Capture one workload with the normal scheduler and save only local diagnostic results."""
        load = self.default_load()
        requests = load_requests(self.config, number=load["number"] + warmup_requests)
        client = self.create_client(load["parallel"], len(requests))
        metrics = None
        failure = None
        # The server's abandonment timer bounds a lost CLI; ordinary completion stops immediately.
        duration = max(1, int(self.config.endpoint.timeout * (self.config.endpoint.max_retries + 1) * len(requests) + 60))
        if load["rate"] > 0:
            duration += int(len(requests) / load["rate"]) + 1
        try:
            raw_output = await self.dispatch(
                client, requests, parallel=load["parallel"], rate=load["rate"],
                open_loop=load["open_loop"], warmup_requests=warmup_requests,
                start_capture=lambda: session.start(duration),
            )
            metrics = self.aggregate_metrics(
                raw_output, rate=load["rate"], number=load["number"],
                resolved_parallel=load["resolved_parallel"], include_user_throughput=False,
            )
        except BaseException as error:
            failure = str(error) or type(error).__name__
            raise
        finally:
            await client.close()
            try:
                session.stop_and_collect()
            except Exception as error:
                if failure is None:
                    failure = str(error)
                    raise
                logging.getLogger(__name__).exception("Could not finish profile collection; captured files remain in the Pods")
            finally:
                session.save_run({**self.config.to_dict(), "warmup_requests": warmup_requests}, metrics, failure)
        return {"mode": "profile", "metrics": metrics, "output_dir": str(session.output_dir)}

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
