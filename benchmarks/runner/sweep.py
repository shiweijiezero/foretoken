# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Parameter × load sweep: expand RunSpecs, run points, summarize, Pareto."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from benchmarks.config import ParamSweepConfig
from benchmarks.logger.wandb import wandb_run_base
from benchmarks.report.pareto import plot_sweep_pareto
from benchmarks.report.summary import log_sweep_results
from benchmarks.runner.base import Runner
from benchmarks.runner.run_benchmark import RunBenchmark
from benchmarks.runner.run_spec import RunSpec
from benchmarks.utils.bench_params import (
    apply_bench_overrides,
    load_param_sweep,
    sanitize_filename,
)

logger = logging.getLogger(__name__)


class SweepRunner(Runner):
    """Expand bench-params × load points and run each via ``RunBenchmark``.

    Owns the experiment-root writer; each point owns its own client, child
    writer, and W&B logger.
    """

    async def run(self) -> dict[str, Any]:
        sweep = self.config.param_sweep
        if sweep.num_runs < 1:
            raise ValueError(f"--num-runs must be >= 1, got {sweep.num_runs}")

        combinations = list(load_param_sweep(sweep.bench_params))
        if not combinations:
            raise ValueError("Parameter sweep contains no combinations")

        experiment_name = sweep.experiment_name.strip().replace("/", "-")
        experiment_dir = (
            os.path.join(self.config.output.output_dir, experiment_name)
            if experiment_name
            else None
        )
        writer = self.create_writer(experiment_dir)
        experiment_dir = writer.output_dir
        wandb_enabled = self.config.output.includes("wandb")
        wandb_group = wandb_run_base(self.config) if wandb_enabled else None

        plan = {
            "mode": "sweep",
            "bench_params": sweep.bench_params,
            "num_runs": sweep.num_runs,
            "experiment_dir": experiment_dir,
            "wandb_group": wandb_group,
            "combinations": [
                {
                    "dir": sanitize_filename(bench_comb.name),
                    "bench": dict(bench_comb),
                }
                for bench_comb in combinations
            ],
            "base": self.config.to_dict(),
        }
        writer.save_json("config.json", plan)

        all_points: list[dict[str, Any]] = []
        for bench_comb in combinations:
            comb_name = sanitize_filename(bench_comb.name)
            comb_root = os.path.join(experiment_dir, comb_name)
            point_config = apply_bench_overrides(self.config, bench_comb)
            point_config.validate()
            point_config = replace(
                point_config,
                param_sweep=ParamSweepConfig(),
            )

            for run_number in range(sweep.num_runs):
                logger.info(
                    "Sweep %s run=%s/%s bench=%s",
                    comb_name,
                    run_number + 1,
                    sweep.num_runs,
                    dict(bench_comb),
                )
                run_dir = os.path.join(comb_root, f"run={run_number}")
                label = (
                    f"{comb_name}-run{run_number}"
                    if sweep.num_runs > 1
                    else comb_name
                )
                result = await RunBenchmark(
                    RunSpec(
                        config=point_config,
                        label=label,
                        output_dir=run_dir,
                        wandb_group=wandb_group,
                    )
                ).run()
                point = dict(result["metrics"])
                point["combination"] = comb_name
                point["parameter_group"] = str(bench_comb["_parameter_group"])
                point["run_number"] = run_number
                point["gpu_count"] = point_config.output.gpu_count
                point["bench"] = dict(bench_comb)
                point["label"] = f"{comb_name}|p={point['parallel']}"
                all_points.append(point)

        if len(all_points) > 1:
            fig_path = plot_sweep_pareto(all_points, writer.output_dir)
            log_sweep_results(all_points)
            logger.info("Pareto plot: %s", fig_path)

        writer.save_json("sweep_points.json", all_points)
        best = max(
            all_points,
            key=lambda item: item["throughput"][
                "generation_tokens_per_second"
            ],
        )
        logger.info(
            "Sweep done: %s combinations, %s points, output_dir=%s",
            len(combinations),
            len(all_points),
            experiment_dir,
        )
        return {
            "mode": "sweep",
            "metrics": best,
            "results": all_points,
            "output_dir": experiment_dir,
            "combinations": len(combinations),
            "wandb_group": wandb_group,
        }
