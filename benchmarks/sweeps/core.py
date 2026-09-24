# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared lifecycle for parameter sweeps across benchmark domains."""

from __future__ import annotations

import csv
import itertools
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar

from benchmarks.datasets.conversations import iter_jsonl_rows
from benchmarks.results.output import result_directory_path, write_json

logger = logging.getLogger(__name__)

ConfigT = TypeVar("ConfigT")
SweepPoint = dict[str, object]
_BENCHMARK_NAME = "_benchmark_name"
_PARAMETER_GROUP = "_parameter_group"


@dataclass(frozen=True)
class SweepDefinition:
    """Describe the JSONL file and repetitions owned by a sweep command."""

    path: str
    num_runs: int
    experiment_name: str


@dataclass
class SweepExecution:
    """Hold generic sweep artifacts and point metrics for a domain adapter."""

    plan: dict[str, Any]
    combinations: list[SweepPoint]
    points: list[dict[str, Any]]
    artifacts: dict[str, Path]
    experiment_dir: str


class SweepAdapter(Protocol, Generic[ConfigT]):
    """Define domain behavior at the shared sweep lifecycle boundary."""

    axis_fields: Mapping[str, Callable[[Any], Any]]

    def validate_record(self, record: SweepPoint, line_no: int) -> None: ...

    def apply_point(self, config: ConfigT, point: SweepPoint) -> ConfigT: ...

    def validate_point(self, config: ConfigT) -> None: ...

    def plan_base(self, config: ConfigT) -> dict[str, Any]: ...

    def group_name(self, config: ConfigT) -> str: ...

    async def execute_point(
        self,
        config: ConfigT,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> Mapping[str, Any]: ...



def sweep_point_name(point: SweepPoint) -> str:
    """Return the explicit name or build a point name in JSONL parameter order."""
    if _BENCHMARK_NAME in point:
        return str(point[_BENCHMARK_NAME])
    return "-".join(
        f"{key}={value}"
        for key, value in point.items()
        if key not in {_BENCHMARK_NAME, _PARAMETER_GROUP}
    ) or "default"



def sweep_directory_name(name: str) -> str:
    """Convert a point name into a safe result directory name."""
    return name.replace("/", "_").replace("..", "__").strip("'\"")



def _load_axis_values(
    record: SweepPoint,
    key: str,
    caster: Callable[[Any], Any],
) -> list[Any] | None:
    if key not in record:
        return None
    value = record[key]
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError(f"Sweep axis {key!r} cannot be empty")
    return [caster(item) for item in values]



def expand_sweep_point(
    record: SweepPoint,
    axis_fields: Mapping[str, Callable[[Any], Any]],
) -> list[SweepPoint]:
    """Expand adapter-owned list fields into Cartesian combinations."""
    axes = {
        key: _load_axis_values(record, key, caster)
        for key, caster in axis_fields.items()
    }
    active = {key: values for key, values in axes.items() if values is not None}
    rest = {
        key: value
        for key, value in record.items()
        if key not in axis_fields and key not in {_BENCHMARK_NAME, _PARAMETER_GROUP}
    }
    base_name = record.get(_BENCHMARK_NAME)
    parameter_group = (
        str(base_name) if base_name is not None else sweep_point_name(rest)
    )
    keys = list(active)
    points: list[SweepPoint] = []
    value_sets = [active[key] for key in keys]
    for values in itertools.product(*value_sets) if value_sets else [()]:
        point = {**rest, **dict(zip(keys, values)), _PARAMETER_GROUP: parameter_group}
        if base_name is not None:
            suffix = "-".join(f"{key}={point[key]}" for key in keys)
            point[_BENCHMARK_NAME] = f"{base_name}-{suffix}" if suffix else str(base_name)
        points.append(point)
    return points



def load_sweep_points(
    definition: SweepDefinition,
    adapter: SweepAdapter,
) -> list[SweepPoint]:
    """Read JSONL, validate domain keys, and expand all sweep combinations."""
    if not definition.path:
        raise ValueError("Parameter sweep requires --sweep PATH")
    points: list[SweepPoint] = []
    directory_names: list[str] = []
    for _, line_no, _, record in iter_jsonl_rows(definition.path, allow_comments=True):
        if not isinstance(record, dict):
            raise TypeError(
                "Each sweep JSONL line must be an object, "
                f"got {type(record)} on line {line_no}"
            )
        adapter.validate_record(record, line_no)
        expanded = expand_sweep_point(record, adapter.axis_fields)
        points.extend(expanded)
        directory_names.extend(
            sweep_directory_name(sweep_point_name(point)) for point in expanded
        )
    duplicates = {name for name, count in Counter(directory_names).items() if count > 1}
    if duplicates:
        raise ValueError(
            "Duplicate sweep output directories: " + ", ".join(sorted(duplicates))
        )
    if not points:
        raise ValueError("Parameter sweep contains no combinations")
    return points


async def run_sweep(
    config: ConfigT,
    definition: SweepDefinition,
    adapter: SweepAdapter[ConfigT],
    *,
    mode: str,
    dry_run: bool = False,
) -> SweepExecution:
    """Run every point through a domain adapter and publish shared sweep artifacts."""
    if definition.num_runs < 1:
        raise ValueError(f"--num-runs must be >= 1, got {definition.num_runs}")
    combinations = load_sweep_points(definition, adapter)
    experiment_name = definition.experiment_name.strip().replace("/", "-")
    experiment_dir = result_directory_path(
        config,
        os.path.join(config.outputs.output_dir, experiment_name)
        if experiment_name
        else None,
    )
    local_enabled = config.outputs.includes("local")
    if local_enabled:
        if experiment_name:
            try:
                os.makedirs(experiment_dir)
            except FileExistsError as error:
                raise ValueError(
                    f"Experiment directory already exists: {experiment_dir}; "
                    "choose a new --experiment-name"
                ) from error
        plan = {
            "mode": mode,
            "sweep": definition.path,
            "num_runs": definition.num_runs,
            "wandb_group": adapter.group_name(config),
            "combinations": [
                {
                    "dir": sweep_directory_name(sweep_point_name(point)),
                    "bench": dict(point),
                }
                for point in combinations
            ],
            "base": adapter.plan_base(config),
        }
        write_json(experiment_dir, "config.json", plan)
    else:
        plan = {
            "mode": mode,
            "sweep": definition.path,
            "num_runs": definition.num_runs,
            "wandb_group": adapter.group_name(config),
            "combinations": [dict(point) for point in combinations],
            "base": adapter.plan_base(config),
        }

    wandb_group = plan["wandb_group"]
    all_points: list[dict[str, Any]] = []
    for combination in combinations:
        combination_name = sweep_directory_name(sweep_point_name(combination))
        point_config = adapter.apply_point(config, combination)
        adapter.validate_point(point_config)
        for run_number in range(definition.num_runs):
            label = (
                f"{combination_name}-run{run_number}"
                if definition.num_runs > 1
                else combination_name
            )
            run_dir = os.path.join(experiment_dir, combination_name, f"run={run_number}")
            logger.info(
                "Sweep %s run=%s/%s bench=%s",
                combination_name,
                run_number + 1,
                definition.num_runs,
                dict(combination),
            )
            metrics = dict(
                await adapter.execute_point(
                    point_config,
                    output_dir=run_dir,
                    label=label,
                    wandb_group=wandb_group,
                    dry_run=dry_run,
                )
            )
            metrics.update(
                {
                    "combination": combination_name,
                    "parameter_group": str(combination[_PARAMETER_GROUP]),
                    "run_number": run_number,
                    "bench": dict(combination),
                    "label": label,
                }
            )
            all_points.append(metrics)

    artifacts: dict[str, Path] = {}
    if local_enabled:
        artifacts["sweep_points"] = write_json(
            experiment_dir, "sweep_points.json", all_points
        )
        summary = summarize_sweep(all_points)
        artifacts["sweep_summary"] = write_json(
            experiment_dir, "sweep_summary.json", summary
        )
        artifacts["sweep_summary_csv"] = write_sweep_csv(summary, experiment_dir)
        logger.info("Repeated-run summaries: %s/sweep_summary.csv", experiment_dir)
    logger.info(
        "Sweep done: %s combinations, %s points, output_dir=%s",
        len(combinations),
        len(all_points),
        experiment_dir,
    )
    return SweepExecution(plan, combinations, all_points, artifacts, experiment_dir)


def _scalar_metrics(value: Any, prefix: str = "") -> dict[str, float | int]:
    """Flatten numeric result leaves for repeated-point comparisons."""
    if isinstance(value, bool):
        return {}
    if isinstance(value, (int, float)):
        return {prefix.rstrip("_"): value} if prefix else {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, float | int] = {}
    for key, child in value.items():
        if key in {"combination", "parameter_group", "bench", "label", "run_number"}:
            continue
        child_prefix = f"{prefix}{key}_"
        if prefix == "throughput_":
            child_prefix = f"{key}_"
        if prefix in {"latency_", "ttft_", "tpot_", "itl_"} and key in {
            "mean",
            "p50",
            "p95",
            "p99",
        }:
            child_prefix = f"{prefix}{key}_seconds_"
        result.update(_scalar_metrics(child, child_prefix))
    return result


def summarize_sweep(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return scalar comparisons without pooling unrelated distributions."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        groups[str(point["combination"])].append(point)
    rows: list[dict[str, Any]] = []
    for combination, runs in groups.items():
        samples: dict[str, list[float | int]] = defaultdict(list)
        for run in runs:
            for metric, value in _scalar_metrics(run).items():
                samples[metric].append(value)
        for metric, values in samples.items():
            numeric = [float(value) for value in values]
            rows.append(
                {
                    "combination": combination,
                    "parameter_group": runs[0]["parameter_group"],
                    "metric": metric,
                    "runs": len(runs),
                    "samples": len(numeric),
                    "mean": mean(numeric),
                    "median": median(numeric),
                    "stddev": stdev(numeric) if len(numeric) > 1 else None,
                    "min": min(numeric),
                    "max": max(numeric),
                }
            )
    return rows


def write_sweep_csv(rows: list[dict[str, Any]], directory: str) -> Path:
    """Write scalar sweep summaries for spreadsheet and plotting tools."""
    path = Path(directory) / "sweep_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "combination",
                "parameter_group",
                "metric",
                "runs",
                "samples",
                "mean",
                "median",
                "stddev",
                "min",
                "max",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


__all__ = [
    "SweepAdapter",
    "SweepDefinition",
    "SweepExecution",
    "SweepPoint",
    "_BENCHMARK_NAME",
    "_PARAMETER_GROUP",
    "run_sweep",
    "sweep_directory_name",
    "sweep_point_name",
    "expand_sweep_point",
    "load_sweep_points",
    "summarize_sweep",
    "write_sweep_csv",
]
