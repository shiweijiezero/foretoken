# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Expand, execute, and summarize parameter sweeps across benchmark domains."""

from __future__ import annotations

import itertools
import os
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from loguru import logger

from benchmarks.datasets.rows import iter_jsonl_rows
from benchmarks.results.measurements.sweeps import summarize_sweep, write_sweep_csv
from benchmarks.results.output import ResultOutputs
from benchmarks.results.recording.tasks import (
    completed_measurement,
    run_task,
    task_source,
)
from benchmarks.results.recovery import prepare_task_resume
from benchmarks.results.reporting.local import LocalDirectorySink
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.recording import WandbSink

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
            from benchmarks.results.wandb.http import publish_sweep_wandb

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
        outputs.open(plan, configuration=plan)
        artifacts["config"] = root / "config.json"
        # Imported task records are references, not new samples. Populate all reused
        # tasks before dispatch so progress also covers completed points later in the plan.
        for point in all_points:
            task_dir = root / point["combination"] / f"run={point['run_number']}"
            saved = completed_measurement(task_dir)
            if saved is not None:
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
            summary = summarize_sweep(all_points, num_runs=definition.num_runs)
            documents = outputs.records.publish({
                "sweep_points.json": all_points, "sweep_summary.json": summary, "metrics.json": totals,
            })
            artifacts.update({Path(name).stem: path for name, path in documents.items()})
            artifacts["sweep_summary_csv"] = write_sweep_csv(summary, experiment_dir)
            if finished:
                log = logger.info if status == "completed" else logger.error
                log("Sweep {}: completed={}/{}, failed={}, interrupted={}, pending={}",
                    status, counts["completed"], len(all_points), counts["failed"], counts["interrupted"], counts["pending"])
            return totals

        checkpoint()
        try:
            for point in all_points:
                if point.get("reused"):
                    logger.info("Reusing completed sweep task: {}/run={}", point["combination"], point["run_number"])
                    continue
                point_config = prepared[point["combination"]][1]
                point_config = replace(point_config, outputs=point_config.outputs.for_child_run())
                run_dir = root / point["combination"] / f"run={point['run_number']}"
                point.update(status="running", exit_code=None)
                checkpoint()
                logger.info("Sweep {} run={}/{}", point["combination"], point["run_number"] + 1, definition.num_runs)
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
                checkpoint()
        except BaseException:
            try:
                checkpoint(finished=True)
            except BaseException:  # noqa: BLE001 - Final checkpoints must not replace the active run failure.
                logger.exception("Could not retain the sweep outcome")
            raise
        totals = checkpoint(finished=True)
        run = BenchmarkRun(plan, totals, None, artifacts,
                           exit_code=0 if totals["sweep"]["status"] == "completed" else 1)
        outputs.publish(run)
    return SweepExecution(run, all_points, experiment_dir)


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
        try:
            active[key] = [caster(item) for item in values]
        except ValueError as error:
            raise ValueError(f"Invalid sweep axis {key!r}: {error}") from error
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
        try:
            adapter.validate_record(record, line_no)
            points.extend(expand_sweep_point(record, adapter.axis_fields))
        except ValueError as error:
            raise ValueError(f"Invalid sweep at {definition.path}:{line_no}: {error}") from error
    names = [sweep_directory_name(sweep_point_name(point)) for point in points]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    if duplicates:
        raise ValueError(
            "Duplicate sweep output directories: " + ", ".join(sorted(duplicates))
        )
    if not points:
        raise ValueError("Parameter sweep contains no combinations")
    return points
