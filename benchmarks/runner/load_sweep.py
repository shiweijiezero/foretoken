# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Run a load sweep over ``(parallel, number, rate)`` points, then plot Pareto."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from benchmarks.report.pareto import plot_load_sweep_pareto
from benchmarks.report.summary import log_sweep_results
from benchmarks.runner.base import Runner, load_point_label
from benchmarks.workload.loader import load_requests

logger = logging.getLogger(__name__)


class LoadSweepRunner(Runner):
    """Sweep concurrency / request count / arrival rate, then plot Pareto."""

    async def run(self) -> dict[str, Any]:
        if self.config.dataset.is_multi:
            raise ValueError(
                "Load sweep cannot be combined with multiple --dataset values"
            )

        points = self.config.load.sweep_points()
        open_loop = self.config.load.open_loop
        gpu_count = int(self.config.output.gpu_count)
        writer = self.make_writer()

        sweep_config = {
            "mode": "load_sweep",
            "model": self.config.target.model,
            "url": self.config.target.url,
            "open_loop": open_loop,
            "stream": self.config.generation.stream,
            "gpu_count": gpu_count,
            "sweep_points": [
                {
                    "parallel": (-1 if open_loop else parallel),
                    "number": number,
                    "rate": rate,
                }
                for parallel, number, rate in points
            ],
        }
        writer.save_json(
            "config.json", {**self.config.to_dict(), **sweep_config}
        )

        wandb_group = self.experiment_wandb_group()

        results: list[dict[str, Any]] = []
        for index, (parallel, number, rate) in enumerate(points):
            parallel, number, rate = int(parallel), int(number), float(rate)
            resolved_parallel = -1 if open_loop else parallel
            point_load = {
                "parallel": parallel,
                "number": number,
                "rate": rate,
                "open_loop": open_loop,
                "resolved_parallel": resolved_parallel,
            }
            child_name = load_point_label(
                resolved_parallel, number, rate, open_loop
            )
            logger.info(
                "Sweep point %s/%s: parallel=%s number=%s rate=%s",
                index + 1,
                len(points),
                "unlimited" if open_loop else parallel,
                number,
                rate,
            )

            point_config = replace(
                self.config,
                load=replace(
                    self.config.load,
                    parallel=[parallel],
                    number=[number],
                    rate=[rate],
                ),
            )
            requests = load_requests(point_config)
            child = writer.child(child_name)
            run_config = self.build_run_config("load_sweep_point", point_load)
            wandb_logger = self.make_wandb_logger(
                child,
                point_load,
                name_suffix=child_name,
                group=wandb_group,
                config=point_config,
            )
            try:
                raw_output = await self.dispatch(
                    self.make_client(parallel, number),
                    requests,
                    parallel=parallel,
                    rate=rate,
                    open_loop=open_loop,
                    wandb_logger=wandb_logger,
                )
                metrics = self.aggregate_metrics(
                    raw_output,
                    rate=rate,
                    number=number,
                    resolved_parallel=resolved_parallel,
                )
                metrics["gpu_count"] = gpu_count
                self.save_results(
                    child,
                    run_config,
                    raw_output,
                    metrics,
                    wandb_logger=wandb_logger,
                    config_snapshot=point_config.to_dict(),
                )
                results.append(metrics)
            except Exception:
                wandb_logger.finish()
                raise

        fig_path, prepared, frontier = plot_load_sweep_pareto(
            results,
            writer.output_dir,
        )
        prepared_by_index = {
            int(row["_foretoken_index"]): row for row in prepared
        }
        for index, metrics in enumerate(results):
            row = prepared_by_index[index]
            metrics["pareto"] = {
                "token_s_per_user": float(row["tokens_per_user"]),
                "token_s_per_gpu": float(row["tokens_per_gpu"]),
                "gpu_count": float(row["gpu_count"]),
            }
            metrics["throughput"]["token/s/user"] = float(
                row["tokens_per_user"]
            )

        log_sweep_results(results)
        logger.info("Pareto plot: %s", fig_path)

        writer.save_json("sweep_points.json", results)
        writer.save_json(
            "pareto_frontier.json",
            {
                "schema": "foretoken.load_sweep.pareto.v1",
                "gpu_count": gpu_count,
                "axes": {
                    "x": "tokens_per_user",
                    "y": "tokens_per_gpu",
                },
                "frontier": frontier,
                "plot": str(fig_path),
            },
        )

        best = max(results, key=lambda item: item["throughput"]["token/s"])
        return {
            "mode": "load_sweep",
            "metrics": best,
            "results": results,
            "pareto_frontier": frontier,
            "pareto_plot": str(fig_path),
            "output_dir": writer.output_dir,
            "wandb_group": wandb_group if self.config.wandb.enabled else None,
        }
