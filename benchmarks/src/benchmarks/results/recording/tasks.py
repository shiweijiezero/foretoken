# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist complete measurement tasks for sweep and SLO-search continuation."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.results.recording.files import write_json
from benchmarks.results.types import BenchmarkRun

logger = logging.getLogger(__name__)


def run_task(
    directory: Path,
    execute: Callable[[], BenchmarkRun],
) -> BenchmarkRun:
    """Reuse a completed task's result, including failures, or execute it from the start.

    Interrupted measurements restart in the new result directory;
    original request records are kept for diagnosis.
    """
    directory.mkdir(parents=True, exist_ok=True)
    previous = completed_measurement(directory)
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


def task_source(directory: Path) -> Path:
    """Resolve a reused task's original artifacts for result readers and later resumptions."""
    path = directory / "task.json"
    if not path.is_file():
        return directory
    task = json.loads(path.read_text(encoding="utf-8"))
    return (directory / task["source"]).resolve() if task["status"] == "completed" else directory


def completed_measurement(directory: Path) -> dict[str, Any] | None:
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
