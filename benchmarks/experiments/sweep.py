# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run parameter combinations against one model service."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from benchmarks.config import HttpBenchmarkConfig, ParameterSweepConfig
from benchmarks.config.sweep import (
    apply_sweep_point,
    load_sweep_points,
    sweep_directory_name,
    sweep_point_name,
)
from benchmarks.deployment import BenchmarkRuntimeEndpoint
from benchmarks.serving.standard import StandardHttpLoadBenchmark
from benchmarks.results.publication import open_local_result_directory
from benchmarks.results.console import log_sweep_results
from benchmarks.results.pareto import plot_sweep_pareto
from benchmarks.results.wandb import wandb_group_name

logger = logging.getLogger(__name__)

class ParameterSweepBenchmark:
    """Own parameter expansion, repeated runs, W&B grouping, and Pareto artifacts."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint

    def run(self) -> dict[str, Any]:
        """Run all parameter points and return the highest-throughput point as the compatible metrics result."""
        sweep = self.benchmark.parameter_sweep
        if sweep.num_runs < 1:
            raise ValueError(f"--num-runs must be >= 1, got {sweep.num_runs}")

        combinations = load_sweep_points(sweep.bench_params)
        if not combinations:
            raise ValueError("Parameter sweep contains no combinations")

        experiment_name = sweep.experiment_name.strip().replace("/", "-")
        experiment_dir = (
            os.path.join(self.benchmark.outputs.output_dir, experiment_name)
            if experiment_name
            else None
        )
        result_directory = open_local_result_directory(self.benchmark, experiment_dir)
        experiment_dir = result_directory.output_dir
        wandb_enabled = self.benchmark.outputs.includes("wandb")
        wandb_group = (
            wandb_group_name(self.benchmark, self.endpoint)
            if wandb_enabled
            else None
        )

        plan = {
            "mode": "parameter_sweep",
            "bench_params": sweep.bench_params,
            "num_runs": sweep.num_runs,
            "wandb_group": wandb_group,
            "combinations": [
                {
                    "dir": sweep_directory_name(sweep_point_name(point)),
                    "bench": dict(point),
                }
                for point in combinations
            ],
            "base": self.benchmark.to_dict(),
        }
        result_directory.save_json("config.json", plan)

        all_points: list[dict[str, Any]] = []
        for combination in combinations:
            combination_name = sweep_directory_name(sweep_point_name(combination))
            combination_root = os.path.join(experiment_dir, combination_name)
            point_benchmark = apply_sweep_point(self.benchmark, combination)
            point_benchmark.validate()
            point_benchmark = replace(
                point_benchmark,
                parameter_sweep=ParameterSweepConfig(),
            )

            for run_number in range(sweep.num_runs):
                logger.info(
                    "Sweep %s run=%s/%s bench=%s",
                    combination_name,
                    run_number + 1,
                    sweep.num_runs,
                    dict(combination),
                )
                run_dir = os.path.join(combination_root, f"run={run_number}")
                label = (
                    f"{combination_name}-run{run_number}"
                    if sweep.num_runs > 1
                    else combination_name
                )
                result = StandardHttpLoadBenchmark(
                    point_benchmark,
                    self.endpoint,
                    label=label,
                    output_dir=run_dir,
                    wandb_group=wandb_group,
                ).run()
                point = dict(result["metrics"])
                point["combination"] = combination_name
                point["parameter_group"] = str(combination["_parameter_group"])
                point["run_number"] = run_number
                point["gpu_count"] = self.endpoint.gpu_count
                if point_benchmark.is_multi_turn:
                    point["multi_turn"] = True
                point["bench"] = dict(combination)
                point["label"] = f"{combination_name}|p={point['parallel']}"
                all_points.append(point)

        if len(all_points) > 1:
            if result_directory.enabled:
                fig_path = plot_sweep_pareto(all_points, result_directory.output_dir)
                logger.info("Pareto plot: %s", fig_path)
            if not self.benchmark.outputs.includes("quiet"):
                log_sweep_results(all_points)

        result_directory.save_json("sweep_points.json", all_points)
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
            "mode": "parameter_sweep",
            "metrics": best,
            "results": all_points,
            "output_dir": experiment_dir,
            "combinations": len(combinations),
            "wandb_group": wandb_group,
        }
