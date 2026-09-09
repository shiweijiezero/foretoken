# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse and run the parameter sweep for the current HTTP benchmark."""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import replace
from typing import Any, Callable

from benchmarks.performance.config import (
    HttpBenchmarkConfig,
    ParameterSweepConfig,
    HttpLoadSchedule,
    normalize_output_token_limit,
)
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint
from benchmarks.performance.conversation import iter_jsonl_rows
from benchmarks.performance.pareto import plot_sweep_pareto
from benchmarks.performance.http_benchmark import StandardHttpLoadBenchmark
from benchmarks.performance.results import open_local_result_directory
from benchmarks.performance.console_output import log_sweep_results
from benchmarks.performance.wandb import wandb_group_name

logger = logging.getLogger(__name__)

SweepPoint = dict[str, object]
_LOAD_CAST = {"parallel": int, "number": int, "rate": float}
_BENCHMARK_NAME = "_benchmark_name"
_PARAMETER_GROUP = "_parameter_group"


def sweep_point_name(point: SweepPoint) -> str:
    """Return the explicit name or build a workload point name in parameter order."""
    if _BENCHMARK_NAME in point:
        return str(point[_BENCHMARK_NAME])
    return "-".join(
        f"{key}={value}"
        for key, value in point.items()
        if key not in {_BENCHMARK_NAME, _PARAMETER_GROUP}
    )


def sweep_directory_name(name: str) -> str:
    """Convert a parameter point name into an existing result directory name."""
    return name.replace("/", "_").replace("..", "__").strip("'\"")


def _dataset_selectors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _preserve_value(value: Any) -> Any:
    return value


# One deployment experiment may change only request and workload choices; endpoint, credentials, traces, and output ownership remain fixed.
_SWEEP_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "parallel": ("load_schedule", "max_concurrency", int),
    "number": ("load_schedule", "request_count", int),
    "rate": ("load_schedule", "arrival_rate", float),
    "open_loop": (
        "load_schedule",
        "unbounded_concurrency",
        _preserve_value,
    ),
    "max_tokens": ("generation", "max_tokens", normalize_output_token_limit),
    "stream": ("generation", "stream", _preserve_value),
    "top_p": ("generation", "top_p", _preserve_value),
    "top_k": ("generation", "top_k", _preserve_value),
    "min_p": ("generation", "min_p", _preserve_value),
    "temperature": ("generation", "temperature", _preserve_value),
    "frequency_penalty": ("generation", "frequency_penalty", _preserve_value),
    "presence_penalty": ("generation", "presence_penalty", _preserve_value),
    "repetition_penalty": ("generation", "repetition_penalty", _preserve_value),
    "extra_body": ("generation", "extra_body", dict),
    "dataset": ("request_dataset", "dataset_selectors", _dataset_selectors),
    "dataset_offset": ("request_dataset", "row_offset", int),
    "tokenizer_path": ("request_dataset", "tokenizer", str),
    "random_seed": ("request_dataset", "random_seed", int),
    "min_prompt_length": (
        "request_dataset",
        "minimum_prompt_tokens",
        int,
    ),
    "max_prompt_length": (
        "request_dataset",
        "maximum_prompt_tokens",
        int,
    ),
    "prefix_length": ("request_dataset", "shared_prefix_tokens", int),
    "prompt": ("request_dataset", "fixed_prompt", str),
    "max_turns": ("request_dataset", "max_turns", int),
}


def _load_axis_values(
    record: dict[str, object],
    key: str,
    caster: type,
) -> list[Any] | None:
    if key not in record:
        return None
    value = record[key]
    if isinstance(value, list):
        return [caster(item) for item in value]
    return [caster(value)]


def _axis_value_for_point(values: list[Any] | None, index: int) -> Any | None:
    if values is None:
        return None
    return values[index] if len(values) > 1 else values[0]


def expand_load_points(item: SweepPoint) -> list[SweepPoint]:
    """Expand list-valued workload axes into scalar parameter points."""
    record = dict(item)
    axes = {
        key: _load_axis_values(record, key, caster)
        for key, caster in _LOAD_CAST.items()
    }
    multi = {
        key: values
        for key, values in axes.items()
        if values is not None and len(values) > 1
    }

    if "rate" in multi and "parallel" in multi:
        raise ValueError(
            "Cannot sweep both rate and parallel in one bench-params line; "
            "pass one multi-value list at a time."
        )

    primary = next(
        (key for key in ("rate", "parallel", "number") if key in multi),
        None,
    )
    count = len(multi[primary]) if primary else 1
    if (
        primary in ("rate", "parallel")
        and "number" in multi
        and len(multi["number"]) != count
    ):
        raise ValueError(
            f"number list must match {primary} length when both are "
            f"multi-value; got number={len(multi['number'])}, {primary}={count}"
        )

    base_name = record.get(_BENCHMARK_NAME)
    rest = {
        key: value
        for key, value in record.items()
        if key not in (*_LOAD_CAST, _BENCHMARK_NAME, _PARAMETER_GROUP)
    }
    parameter_group = (
        str(base_name)
        if base_name is not None
        else sweep_point_name(rest) or "default"
    )

    results: list[SweepPoint] = []
    for index in range(count):
        point = {
            **rest,
            **{
                key: _axis_value_for_point(values, index)
                for key, values in axes.items()
                if values is not None
            },
        }
        parallel = point.get("parallel")
        rate = point.get("rate")
        number = point.get("number")
        if parallel is not None or rate is not None:
            HttpLoadSchedule.validate_coordinates(
                max_concurrency=(
                    int(parallel) if parallel is not None else 1
                ),
                arrival_rate=float(rate) if rate is not None else -1.0,
            )
        if number is not None and int(number) < 1:
            raise ValueError(f"number must be >= 1, got {number}")

        point[_PARAMETER_GROUP] = parameter_group
        if base_name is not None:
            if count > 1:
                rate_value = point.get("rate")
                if rate_value is None:
                    rate_tag = "x"
                elif float(rate_value) == -1:
                    rate_tag = "-1"
                else:
                    rate_tag = f"{float(rate_value):g}"
                point[_BENCHMARK_NAME] = (
                    f"{base_name}"
                    f"-p{point.get('parallel', 'x')}"
                    f"-n{point.get('number', 'x')}"
                    f"-r{rate_tag}"
                )
            else:
                point[_BENCHMARK_NAME] = str(base_name)
        results.append(point)
    return results


def load_sweep_points(path: str) -> list[SweepPoint]:
    """Read JSONL and expand it into executable HTTP benchmark points."""
    if not path:
        raise ValueError("Parameter sweep requires --bench-params PATH")

    points: list[SweepPoint] = []
    explicit_names: list[str] = []
    for _, line_no, _, record in iter_jsonl_rows(path, allow_comments=True):
        if not isinstance(record, dict):
            raise TypeError(
                "Each bench-params JSONL line must be an object, "
                f"got {type(record)} on line {line_no}"
            )
        expanded = expand_load_points(record)
        points.extend(expanded)
        explicit_names.extend(
            str(point[_BENCHMARK_NAME])
            for point in expanded
            if _BENCHMARK_NAME in point
        )

    duplicates = {
        name for name, count in Counter(explicit_names).items() if count > 1
    }
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate benchmark names: {names}")
    return points


def apply_sweep_point(
    benchmark: HttpBenchmarkConfig,
    sweep_point: SweepPoint,
) -> HttpBenchmarkConfig:
    """Copy the benchmark configuration and apply an allowlisted parameter point."""
    section_updates: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in sweep_point.items():
        if raw_key in {_BENCHMARK_NAME, _PARAMETER_GROUP}:
            continue
        field = _SWEEP_FIELDS.get(str(raw_key))
        if field is None:
            allowed = ", ".join(sorted(_SWEEP_FIELDS))
            raise ValueError(
                f"Unsupported bench-params key {raw_key!r}. "
                "Only fields that change request execution may be swept; "
                f"allowed keys: {allowed}"
            )
        section, attribute, coerce = field
        section_updates.setdefault(section, {})[attribute] = coerce(raw_value)

    updated_benchmark = benchmark
    for section, updates in section_updates.items():
        section_value = getattr(updated_benchmark, section)
        updated_benchmark = replace(
            updated_benchmark,
            **{section: replace(section_value, **updates)},
        )
    return updated_benchmark


class ParameterSweepBenchmark:
    """Own parameter expansion, repeated runs, W&B grouping, and Pareto artifacts."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint

    async def run(self) -> dict[str, Any]:
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
                result = await StandardHttpLoadBenchmark(
                    point_benchmark,
                    self.endpoint,
                    label=label,
                    output_dir=run_dir,
                    wandb_group=wandb_group,
                ).run()
                point = dict(result["metrics"])
                point["combination"] = combination_name
                point["parameter_group"] = str(combination[_PARAMETER_GROUP])
                point["run_number"] = run_number
                point["gpu_count"] = self.endpoint.gpu_count
                if point_benchmark.is_multi_turn:
                    point["multi_turn"] = True
                point["bench"] = dict(combination)
                point["label"] = f"{combination_name}|p={point['parallel']}"
                all_points.append(point)

        if len(all_points) > 1:
            fig_path = plot_sweep_pareto(all_points, result_directory.output_dir)
            log_sweep_results(all_points)
            logger.info("Pareto plot: %s", fig_path)

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
