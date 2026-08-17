# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Bench-params parameter sweep."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from benchmarks.config import ParamSweepConfig
from benchmarks.report.wandb_logger import wandb_timestamp
from benchmarks.runner.base import Runner
from benchmarks.storage.result_writer import ResultWriter
from benchmarks.sweep.params import (
    ParameterSweepItem,
    apply_bench_overrides,
    load_param_sweep,
    sanitize_filename,
)

logger = logging.getLogger(__name__)


def _comb_dir_name(bench_comb: ParameterSweepItem) -> str:
    if bench_comb:
        return sanitize_filename(bench_comb.name)
    return "BASE"


def _collect_metric_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    mode = result["mode"]
    if mode == "load_sweep":
        return list(result["results"])
    if mode in ("run_benchmark", "multi_dataset"):
        return [result["metrics"]]
    raise ValueError(f"Unsupported nested benchmark mode for param sweep: {mode}")


class ParamSweepRunner(Runner):
    """Run bench-params combinations against an existing service."""

    async def run(self) -> dict[str, Any]:
        sweep = self.config.param_sweep
        if sweep.num_runs < 1:
            raise ValueError(f"--num-runs must be >= 1, got {sweep.num_runs}")

        bench_sweep = load_param_sweep(sweep.bench_params)
        combinations = list(bench_sweep)
        if not combinations:
            raise ValueError("No bench-params combinations to run")

        wandb_group = self.experiment_wandb_group()
        experiment_name = sweep.experiment_name.strip()
        if not experiment_name:
            experiment_name = wandb_timestamp()
        experiment_name = experiment_name.replace("/", "-")
        experiment_dir = os.path.join(
            self.config.output.outputs_dir, experiment_name
        )
        writer = ResultWriter(output_dir=experiment_dir)

        plan = {
            "mode": "param_sweep",
            "bench_params": sweep.bench_params,
            "num_runs": sweep.num_runs,
            "dry_run": sweep.dry_run,
            "experiment_dir": experiment_dir,
            "wandb_group": wandb_group if self.config.wandb.enabled else None,
            "combinations": [
                {
                    "dir": _comb_dir_name(bench_comb),
                    "bench": dict(bench_comb),
                }
                for bench_comb in combinations
            ],
            "base": self.config.to_dict(),
        }
        writer.save_json("config.json", plan)

        if sweep.dry_run:
            logger.info(
                "Dry run: %s combinations under %s",
                len(combinations),
                experiment_dir,
            )
            for index, entry in enumerate(plan["combinations"]):
                logger.info(
                    "[%s] %s bench=%s",
                    index,
                    entry["dir"],
                    entry["bench"],
                )
            return {
                "mode": "param_sweep",
                "dry_run": True,
                "combinations": len(combinations),
                "output_dir": experiment_dir,
            }

        # Local import avoids a circular dependency with select_runner.
        from benchmarks.runner.select_runner import select_runner

        all_points: list[dict[str, Any]] = []
        for bench_comb in combinations:
            comb_name = _comb_dir_name(bench_comb)
            comb_root = os.path.join(experiment_dir, comb_name)
            os.makedirs(comb_root, exist_ok=True)

            for run_number in range(sweep.num_runs):
                logger.info(
                    "Param sweep %s run=%s/%s bench=%s",
                    comb_name,
                    run_number + 1,
                    sweep.num_runs,
                    dict(bench_comb),
                )
                run_dir = os.path.join(comb_root, f"run={run_number}")
                run_suffix = (
                    f"{comb_name}-run{run_number}"
                    if sweep.num_runs > 1
                    else comb_name
                )
                point_config = apply_bench_overrides(self.config, bench_comb)
                point_config = replace(
                    point_config,
                    param_sweep=ParamSweepConfig(),
                    output=replace(
                        point_config.output,
                        outputs_dir=run_dir,
                    ),
                    wandb=replace(
                        point_config.wandb,
                        group=wandb_group,
                        run_suffix=run_suffix,
                    ),
                )
                result = await select_runner(point_config).run()
                for metrics in _collect_metric_points(result):
                    point = dict(metrics)
                    point["gpu_count"] = int(point_config.output.gpu_count)
                    point["combination"] = comb_name
                    point["run_number"] = run_number
                    point["bench"] = dict(bench_comb)
                    point["label"] = (
                        f"{comb_name}|p={point['parallel']}"
                    )
                    all_points.append(point)

        if not all_points:
            raise ValueError("Param sweep produced no metric points")

        writer.save_json("sweep_points.json", all_points)
        best = max(
            all_points, key=lambda item: item["throughput"]["token/s"]
        )
        logger.info(
            "Param sweep done: %s combinations, %s points, output_dir=%s",
            len(combinations),
            len(all_points),
            experiment_dir,
        )
        return {
            "mode": "param_sweep",
            "metrics": best,
            "results": all_points,
            "output_dir": experiment_dir,
            "combinations": len(combinations),
            "wandb_group": wandb_group if self.config.wandb.enabled else None,
        }
