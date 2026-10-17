# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Expand, execute, and summarize parameter sweeps across benchmark domains."""

from __future__ import annotations

import csv
import itertools
import json
import logging
import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Generic, Protocol, TypeVar

from benchmarks.datasets.conversations import iter_jsonl_rows
from benchmarks.results.output import (
    BenchmarkRun,
    LocalDirectorySink,
    ResultOutputs,
    ResultSink,
    WandbSink,
    write_json,
)
from benchmarks.results.tasks import prepare_task_resume, run_task, task_source

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
    resume: str = ""


@dataclass
class SweepExecution:
    """Hold sweep artifacts and point metrics for a domain adapter."""

    run: BenchmarkRun
    points: list[dict[str, Any]]
    experiment_dir: str


class SweepAdapter(Protocol, Generic[ConfigT]):
    """Define domain behavior at the sequential sweep lifecycle boundary."""

    axis_fields: Mapping[str, Callable[[Any], Any]]

    def validate_record(self, record: SweepPoint, line_no: int) -> None: ...
    def apply_point(self, config: ConfigT, point: SweepPoint) -> ConfigT: ...
    def validate_point(self, config: ConfigT) -> None: ...
    def plan_base(self, config: ConfigT) -> dict[str, Any]: ...
    def group_name(self, config: ConfigT) -> str: ...

    def execute_point(
        self,
        config: ConfigT,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> BenchmarkRun: ...


def _axis_label(value: object) -> str:
    """Use a named compound choice's label rather than its full configuration in paths."""
    if isinstance(value, dict) and "name" in value:
        return str(value["name"])
    if isinstance(value, list) and all(isinstance(group, dict) for group in value):
        return "-".join(
            "+".join(f"{metric}{criterion}" for metric, criterion in group.items())
            for group in value
        )
    return str(value)


def sweep_point_name(point: SweepPoint) -> str:
    """Return the explicit name or build a point name in JSONL parameter order."""
    if _BENCHMARK_NAME in point:
        return str(point[_BENCHMARK_NAME])
    return (
        "-".join(
            f"{key}={_axis_label(value)}"
            for key, value in point.items()
            if key not in {_BENCHMARK_NAME, _PARAMETER_GROUP}
        )
        or "default"
    )


def sweep_directory_name(name: str) -> str:
    """Convert a point name into a safe result directory name."""
    return name.replace("/", "_").replace("..", "__").strip("'\"")


def expand_sweep_point(
    record: SweepPoint,
    axis_fields: Mapping[str, Callable[[Any], Any]],
) -> list[SweepPoint]:
    """Expand adapter-owned list fields; each JSONL row remains a separate parameter group."""
    active: dict[str, list[Any]] = {}
    for key, caster in axis_fields.items():
        if key not in record:
            continue
        value = record[key]
        values = value if isinstance(value, list) else [value]
        if not values:
            raise ValueError(f"Sweep axis {key!r} cannot be empty")
        active[key] = [caster(item) for item in values]
    rest = {
        key: value
        for key, value in record.items()
        if key not in axis_fields and key not in {_BENCHMARK_NAME, _PARAMETER_GROUP}
    }
    base_name = record.get(_BENCHMARK_NAME)
    parameter_group = (
        str(base_name) if base_name is not None else sweep_point_name(rest)
    )
    points = []
    for values in itertools.product(*active.values()):
        point = {**rest, **dict(zip(active, values)), _PARAMETER_GROUP: parameter_group}
        if base_name is not None:
            suffix = "-".join(f"{key}={_axis_label(point[key])}" for key in active)
            point[_BENCHMARK_NAME] = (
                f"{base_name}-{suffix}" if suffix else str(base_name)
            )
        points.append(point)
    return points


def load_sweep_points(
    definition: SweepDefinition, adapter: SweepAdapter
) -> list[SweepPoint]:
    """Read JSONL, validate domain keys, and expand all sweep combinations."""
    if not definition.path:
        raise ValueError("Parameter sweep requires --sweep PATH")
    points: list[SweepPoint] = []
    for _, line_no, _, record in iter_jsonl_rows(definition.path, allow_comments=True):
        if not isinstance(record, dict):
            raise TypeError(
                f"Each sweep JSONL line must be an object, got {type(record)} on line {line_no}"
            )
        adapter.validate_record(record, line_no)
        points.extend(expand_sweep_point(record, adapter.axis_fields))
    names = [sweep_directory_name(sweep_point_name(point)) for point in points]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    if duplicates:
        raise ValueError(
            "Duplicate sweep output directories: " + ", ".join(sorted(duplicates))
        )
    if not points:
        raise ValueError("Parameter sweep contains no combinations")
    return points


def run_sweep(
    config: ConfigT,
    definition: SweepDefinition,
    adapter: SweepAdapter[ConfigT],
    *,
    mode: str,
    dry_run: bool = False,
    combinations: list[SweepPoint] | None = None,
    service_resources: ExitStack | None = None,
) -> SweepExecution:
    """Run points sequentially and publish one comparison through the existing result lifecycle.

    Adapters own request event loops and acquire serving resources in the supplied
    stack. Release those services before result sinks close so final logs include
    cleanup. Completed point records survive a later failure or interruption.
    """
    if definition.num_runs < 1:
        raise ValueError(f"--num-runs must be >= 1, got {definition.num_runs}")
    combinations = (
        load_sweep_points(definition, adapter) if combinations is None else combinations
    )
    prepared = {
        f"point-{index}": (point, adapter.apply_point(config, point))
        for index, point in enumerate(combinations, start=1)
    }
    for _, point_config in prepared.values():
        adapter.validate_point(point_config)
    name = definition.experiment_name.strip().replace("/", "-")
    plan = {
        "mode": mode,
        "sweep": definition.path,
        "num_runs": definition.num_runs,
        "dry_run": dry_run,
        "wandb_group": adapter.group_name(config),
        "combinations": [
            {"dir": point_name, "bench": point}
            for point_name, (point, _) in prepared.items()
        ],
        "base": adapter.plan_base(config),
    }

    def sinks(execution_dir: str) -> list[ResultSink]:
        """Publish the aggregate separately from individual measurement runs."""
        selected: list[ResultSink] = []
        if config.outputs.saves_local:
            selected.append(LocalDirectorySink(execution_dir))
        if config.outputs.includes("wandb") and not dry_run:
            from benchmarks.results.wandb import publish_sweep_wandb

            selected.append(
                WandbSink(
                    config,
                    execution_dir=execution_dir,
                    run_name=f"{config.wandb.run_name or name or Path(execution_dir).name}_comparison",
                    group=plan["wandb_group"],
                    publisher=publish_sweep_wandb,
                    run_config=plan,
                )
            )
        return selected

    all_points = [{
        "combination": combination_name,
        "parameter_group": str(combination[_PARAMETER_GROUP]),
        "run_number": run_number,
        "bench": dict(combination),
        "label": f"{combination_name}-run{run_number}" if definition.num_runs > 1 else combination_name,
        "status": "pending", "exit_code": None,
    } for combination_name, (combination, _) in prepared.items() for run_number in range(definition.num_runs)]
    artifacts: dict[str, Path] = {}
    with (
        ResultOutputs(config, None, directory_prefix=f"{name}-" if name else "", sink_factory=sinks) as outputs,
        service_resources if service_resources is not None else nullcontext(),
    ):
        experiment_dir = outputs.execution_dir
        root = Path(experiment_dir)
        if definition.resume:
            prepare_task_resume(Path(definition.resume), root, plan, outputs=config.outputs)
            plan["resumed_from"] = str(Path(definition.resume).resolve())
        artifacts["config"] = write_json(experiment_dir, "config.json", plan)
        outputs.open(plan)
        # Imported task records are references, not new samples. Populate all reused
        # tasks before dispatch so progress also covers completed points later in the plan.
        for point in all_points:
            task_dir = root / point["combination"] / f"run={point['run_number']}"
            task_path = task_dir / "task.json"
            if task_path.is_file():
                saved = json.loads(task_path.read_text(encoding="utf-8"))
                if saved["status"] == "completed":
                    point.update(saved["metrics"], status="failed" if saved["exit_code"] else "completed",
                                 exit_code=saved["exit_code"], reused=True)
                    point["result"] = os.path.relpath(task_source(task_dir), root)

        def checkpoint(*, finished: bool = False) -> dict[str, Any]:
            """Publish the current task inventory and derived metrics after each transition."""
            counts = Counter(point["status"] for point in all_points)
            status = "running" if not finished else (
                "completed" if counts["completed"] == len(all_points)
                else "partial" if counts["completed"] else "failed"
            )
            totals = {key: sum(int(point.get(key, 0)) for point in all_points)
                      for key in ("request_num", "success_num", "failed_num")}
            totals["sweep"] = {"status": status, "tasks": len(all_points),
                               **{state: counts[state] for state in ("completed", "failed", "interrupted", "running", "pending")}}
            artifacts["sweep_points"] = write_json(experiment_dir, "sweep_points.json", all_points)
            summary = summarize_sweep(all_points, num_runs=definition.num_runs)
            artifacts["sweep_summary"] = write_json(experiment_dir, "sweep_summary.json", summary)
            artifacts["sweep_summary_csv"] = write_sweep_csv(summary, experiment_dir)
            artifacts["metrics"] = write_json(experiment_dir, "metrics.json", totals)
            if finished:
                log = logger.info if status == "completed" else logger.error
                log("Sweep %s: completed=%s/%s, failed=%s, interrupted=%s, pending=%s",
                    status, counts["completed"], len(all_points), counts["failed"], counts["interrupted"], counts["pending"])
            return totals

        checkpoint()
        try:
            for point in all_points:
                if point.get("reused"):
                    logger.info("Reusing completed sweep task: %s/run=%s", point["combination"], point["run_number"])
                    continue
                point_config = prepared[point["combination"]][1]
                point_config = replace(point_config, outputs=point_config.outputs.for_child_run())
                run_dir = root / point["combination"] / f"run={point['run_number']}"
                point.update(status="running", exit_code=None)
                checkpoint()
                logger.info("Sweep %s run=%s/%s", point["combination"], point["run_number"] + 1, definition.num_runs)
                try:
                    result = run_task(run_dir, partial(
                        adapter.execute_point, point_config, output_dir=str(run_dir), label=point["label"],
                        wandb_group=plan["wandb_group"], dry_run=dry_run,
                    ))
                    point.update(result.metrics, exit_code=result.exit_code,
                                 status="completed" if result.exit_code == 0 else "failed")
                    point["result"] = os.path.relpath(task_source(run_dir), root)
                except BaseException as error:
                    point.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                                 exit_code=130 if isinstance(error, KeyboardInterrupt) else 1,
                                 error_type=type(error).__name__)
                    raise
                finally:
                    checkpoint()
        finally:
            totals = checkpoint(finished=True)
        run = BenchmarkRun(plan, totals, None, artifacts,
                           exit_code=0 if totals["sweep"]["status"] == "completed" else 1)
        outputs.publish(run)
    return SweepExecution(run, all_points, experiment_dir)


def _scalar_metrics(value: Any, prefix: str = "") -> dict[str, float | int | None]:
    """Flatten numeric leaves, retaining missing measurements as absent samples."""
    if isinstance(value, bool):
        return {}
    if value is None or isinstance(value, (int, float)):
        measured = value if value is not None and math.isfinite(value) else None
        return {prefix.rstrip("_"): measured} if prefix else {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, float | int | None] = {}
    for key, child in value.items():
        if key in {
            "combination",
            "parameter_group",
            "bench",
            "label",
            "run_number",
            "exit_code",
        }:
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


def summarize_sweep(
    points: list[dict[str, Any]], *, num_runs: int | None = None
) -> list[dict[str, Any]]:
    """Summarize each measured scalar across repetitions, keeping conditions and missing samples."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        method = str(point.get("bench", {}).get("service", {}).get("name", ""))
        groups[method, str(point["combination"])].append(point)
    rows: list[dict[str, Any]] = []
    for (method, combination), runs in groups.items():
        samples: dict[str, list[float | int]] = defaultdict(list)
        for run in runs:
            for metric, value in _scalar_metrics(run).items():
                samples.setdefault(metric, [])
                if value is not None:
                    samples[metric].append(value)
        identity = {
            "combination": combination,
            "parameter_group": runs[0]["parameter_group"],
            "method": method,
            "bench": runs[0]["bench"],
            "runs": sum(run.get("status") != "pending" for run in runs),
            "requested_runs": num_runs if num_runs is not None else len(runs),
            "failed_runs": sum(
                bool(run.get("exit_code")) or bool(run.get("failed_num", 0))
                for run in runs
            ),
        }
        for metric, values in samples.items():
            numeric = [float(value) for value in values]
            rows.append(
                {
                    **identity,
                    "metric": metric,
                    "samples": len(numeric),
                    "mean": mean(numeric) if numeric else None,
                    "median": median(numeric) if numeric else None,
                    "stddev": stdev(numeric) if len(numeric) > 1 else None,
                    "min": min(numeric) if numeric else None,
                    "max": max(numeric) if numeric else None,
                }
            )
        if not samples:
            rows.append(
                {
                    **identity,
                    "metric": "",
                    "samples": 0,
                    "mean": None,
                    "median": None,
                    "stddev": None,
                    "min": None,
                    "max": None,
                }
            )
    return rows


def write_sweep_csv(rows: list[dict[str, Any]], directory: str) -> Path:
    """Write repeat statistics and experimental conditions for spreadsheet consumers."""
    path = Path(directory) / "sweep_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "combination",
                "parameter_group",
                "method",
                "bench",
                "runs",
                "requested_runs",
                "failed_runs",
                "metric",
                "samples",
                "mean",
                "median",
                "stddev",
                "min",
                "max",
            ),
        )
        writer.writeheader()
        writer.writerows(
            {**row, "bench": json.dumps(row["bench"], ensure_ascii=False)}
            for row in rows
        )
    return path
