# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist complete measurement tasks for sweep and SLO-search continuation."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.config.benchmark import BenchmarkOutputConfig
from benchmarks.results.output import BenchmarkRun, write_json

logger = logging.getLogger(__name__)


def task_source(directory: Path) -> Path:
    """Resolve a reused task's original artifacts for result readers and later resumptions."""
    path = directory / "task.json"
    if not path.is_file():
        return directory
    task = json.loads(path.read_text(encoding="utf-8"))
    return (directory / task["source"]).resolve() if task["status"] == "completed" else directory


def _completed_measurement(directory: Path) -> dict[str, Any] | None:
    """Read a completed measurement for reuse, even if saving its outputs failed."""
    path = directory / "task.json"
    if not path.is_file():
        return None
    task = json.loads(path.read_text(encoding="utf-8"))
    origin = task_source(directory)
    context_path = origin / "context.json"
    if context_path.is_file():
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if context["measurement_exit_code"] is not None:
            metrics_path = origin / "metrics.json"
            config_path = origin / "config.json"
            if not metrics_path.is_file() or not config_path.is_file():
                raise ValueError(f"Cannot resume completed measurement: metrics.json or config.json is missing in {origin}")
            return {
                **task, "status": "completed", "source": ".",
                "exit_code": context["measurement_exit_code"],
                "record": task["record"] if "record" in task else json.loads(config_path.read_text(encoding="utf-8")),
                "metrics": task["metrics"] if "metrics" in task else json.loads(metrics_path.read_text(encoding="utf-8")),
            }
    return task if task["status"] == "completed" else None


def prepare_task_resume(
    source: Path, destination: Path, plan: dict[str, Any], *, outputs: BenchmarkOutputConfig,
) -> None:
    """Import completed task records into a fresh run, leaving original measurements untouched.

    The recorded workload plan must match; output destinations and tracking names may
    change. Records reference original artifacts instead of copying videos or traces.
    Nested probe records remain available even when their enclosing search failed.
    """
    source = source.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError("Resume output must be outside the original result directory")
    previous = json.loads((source / "config.json").read_text(encoding="utf-8"))

    def workload(value: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value[key] for key in ("mode", "combinations", "num_runs", "dry_run") if key in value},
            "base": {key: item for key, item in value["base"].items() if key not in {"output", "wandb", "sweep"}},
        }

    if "base" not in previous or workload(previous) != workload(plan):
        raise ValueError("Resume requires the same workload, service choices, and repetitions as the saved run")
    for path in source.rglob("task.json"):
        task = _completed_measurement(path.parent)
        if task is None:
            continue
        origin = task_source(path.parent)
        if not origin.is_dir():
            raise ValueError(f"Original task artifacts are missing: {origin}")
        if (origin / "context.json").is_file():
            from benchmarks.results.publication import recover_publication

            recover_publication(origin, outputs.for_child_run())
        target = destination / path.parent.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        write_json(str(target), "task.json", {
            **task, "source": os.path.relpath(origin, target),
        })


def run_task(
    directory: Path,
    execute: Callable[[], BenchmarkRun],
) -> BenchmarkRun:
    """Reuse a completed task's result, including failures, or execute it from the start.

    Interrupted measurements restart in the new result directory;
    original request records are kept for diagnosis.
    """
    directory.mkdir(parents=True, exist_ok=True)
    previous = _completed_measurement(directory)
    if previous is not None:
        logger.info("Reusing completed measurement: %s", task_source(directory))
        return BenchmarkRun(previous["record"], previous["metrics"], None, {}, exit_code=previous["exit_code"])

    task: dict[str, Any] = {"status": "running", "exit_code": None, "source": "."}
    write_json(str(directory), "task.json", task)
    try:
        run = execute()
    except BaseException as error:
        task.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            exit_code=130 if isinstance(error, KeyboardInterrupt) else 1,
            error_type=type(error).__name__,
        )
        write_json(str(directory), "task.json", task)
        raise
    write_json(str(directory), "task.json", {
        **task, "status": "completed", "exit_code": run.exit_code,
        "record": run.record, "metrics": run.metrics,
    })
    return run
