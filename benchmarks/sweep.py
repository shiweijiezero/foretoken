# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read JSONL sweep points, expand them, and run each one against a single model service."""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from benchmarks.config.benchmark import (
    BenchmarkConfig,
    ParameterSweepConfig,
    normalize_output_token_limit,
)
from benchmarks.episodes.generated_load import GeneratedLoadBenchmark
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_sweep_results
from benchmarks.results.output import (
    BenchmarkRun,
    result_directory_path,
    write_json,
)
from benchmarks.results.pareto import plot_sweep_pareto
from benchmarks.results.wandb import wandb_group_name
from benchmarks.tasks.conversations import iter_jsonl_rows

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


# One deployment experiment may change only request and workload choices; the service, credentials, traces, and output ownership remain fixed.
_SWEEP_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "parallel": ("load", "max_concurrency", int),
    "number": ("load", "request_count", int),
    "rate": ("load", "arrival_rate", float),
    "open_loop": ("load", "unbounded_concurrency", _preserve_value),
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
    "dataset": ("workload", "dataset_selectors", _dataset_selectors),
    "dataset_offset": ("workload", "row_offset", int),
    "tokenizer_path": ("workload", "tokenizer", str),
    "random_seed": ("workload", "random_seed", int),
    "min_prompt_length": ("workload", "minimum_prompt_tokens", int),
    "max_prompt_length": ("workload", "maximum_prompt_tokens", int),
    "prefix_length": ("workload", "shared_prefix_tokens", int),
    "prompt": ("workload", "fixed_prompt", str),
    "max_turns": ("workload", "max_turns", int),
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
        if not value:
            raise ValueError(f"Sweep axis {key!r} cannot be empty")
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
            "Cannot sweep both rate and parallel in one sweep line; "
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
        raise ValueError("Parameter sweep requires --sweep PATH")

    points: list[SweepPoint] = []
    explicit_names: list[str] = []
    for _, line_no, _, record in iter_jsonl_rows(path, allow_comments=True):
        if not isinstance(record, dict):
            raise TypeError(
                "Each sweep JSONL line must be an object, "
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
    benchmark: BenchmarkConfig,
    sweep_point: SweepPoint,
) -> BenchmarkConfig:
    """Copy the benchmark configuration and apply an allowlisted parameter point."""
    section_updates: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in sweep_point.items():
        if raw_key in {_BENCHMARK_NAME, _PARAMETER_GROUP}:
            continue
        field = _SWEEP_FIELDS.get(str(raw_key))
        if field is None:
            allowed = ", ".join(sorted(_SWEEP_FIELDS))
            raise ValueError(
                f"Unsupported sweep key {raw_key!r}. "
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
        benchmark: BenchmarkConfig,
        service: ModelService,
    ) -> None:
        self.benchmark = benchmark
        self.service = service

    def run(self) -> BenchmarkRun:
        """Run all parameter points and return the highest-throughput point as the result metrics."""
        sweep = self.benchmark.sweep
        if sweep.num_runs < 1:
            raise ValueError(f"--num-runs must be >= 1, got {sweep.num_runs}")

        combinations = load_sweep_points(sweep.path)
        if not combinations:
            raise ValueError("Parameter sweep contains no combinations")

        experiment_name = sweep.experiment_name.strip().replace("/", "-")
        experiment_dir = result_directory_path(
            self.benchmark,
            os.path.join(self.benchmark.outputs.output_dir, experiment_name)
            if experiment_name
            else None,
        )
        local_enabled = self.benchmark.outputs.includes("local")
        wandb_group = (
            wandb_group_name(self.benchmark, self.service)
            if self.benchmark.outputs.includes("wandb")
            else None
        )

        plan = {
            "mode": "parameter_sweep",
            "sweep": sweep.path,
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
        if local_enabled:
            os.makedirs(experiment_dir, exist_ok=True)
            write_json(experiment_dir, "config.json", plan)

        all_points: list[dict[str, Any]] = []
        for combination in combinations:
            combination_name = sweep_directory_name(sweep_point_name(combination))
            combination_root = os.path.join(experiment_dir, combination_name)
            point_benchmark = apply_sweep_point(self.benchmark, combination)
            point_benchmark.validate()
            point_benchmark = replace(
                point_benchmark,
                sweep=ParameterSweepConfig(),
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
                result = GeneratedLoadBenchmark(
                    point_benchmark,
                    self.service,
                    label=label,
                    output_dir=run_dir,
                    wandb_group=wandb_group,
                ).run()
                point = dict(result.metrics)
                point["combination"] = combination_name
                point["parameter_group"] = str(combination["_parameter_group"])
                point["run_number"] = run_number
                point["gpu_count"] = self.service.gpu_count
                if point_benchmark.is_multi_turn:
                    point["multi_turn"] = True
                point["bench"] = dict(combination)
                point["label"] = f"{combination_name}|p={point['parallel']}"
                all_points.append(point)

        artifacts: dict[str, Path] = {}
        if len(all_points) > 1:
            if local_enabled:
                fig_path = plot_sweep_pareto(all_points, experiment_dir)
                artifacts["pareto"] = fig_path
                logger.info("Pareto plot: %s", fig_path)
            if not self.benchmark.outputs.includes("quiet"):
                log_sweep_results(all_points)

        if local_enabled:
            write_json(experiment_dir, "sweep_points.json", all_points)
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
        return BenchmarkRun(
            record=plan,
            metrics=best,
            measurements=None,
            artifacts=artifacts,
        )
