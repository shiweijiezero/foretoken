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

from benchmarks.results.output import BenchmarkRun, write_json

logger = logging.getLogger(__name__)


def task_source(directory: Path) -> Path:
    """Resolve a reused task's original artifacts for result readers and later resumptions."""
    path = directory / "task.json"
    if not path.is_file():
        return directory
    task = json.loads(path.read_text(encoding="utf-8"))
    return (directory / task["source"]).resolve() if task["status"] == "completed" else directory


def prepare_task_resume(source: Path, destination: Path, plan: dict[str, Any]) -> None:
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
        task = json.loads(path.read_text(encoding="utf-8"))
        if task["status"] != "completed":
            continue
        origin = task_source(path.parent)
        if not origin.is_dir():
            raise ValueError(f"Original task artifacts are missing: {origin}")
        target = destination / path.parent.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        write_json(str(target), "task.json", {
            **task, "source": os.path.relpath(origin, target),
        })


def run_task(
    directory: Path,
    execute: Callable[[], BenchmarkRun],
    *,
    reuse_failed_measurements: bool = False,
) -> BenchmarkRun:
    """Run or reuse one complete measurement and record its outcome before returning.

    Sweep tasks retry failed outcomes. SLO probes also reuse completed negative
    measurements because they determine the search boundary. Exceptions and interrupted
    measurements are always rerun in full; request journals are diagnostic evidence.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "task.json"
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["status"] == "completed" and (previous["exit_code"] == 0 or reuse_failed_measurements):
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
