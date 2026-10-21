# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist complete measurement tasks for sweep and SLO-search continuation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.results.recording.database import (
    DATABASE_FILENAME,
    read_records,
    write_records,
)
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.types import BenchmarkRun

logger = logging.getLogger(__name__)


def run_task(
    directory: Path,
    execute: Callable[[], BenchmarkRun],
) -> BenchmarkRun:
    """Reuse a completed task, including measured failures, or execute it from the start.

    Interrupted measurements restart in the new result directory;
    original request records are kept for diagnosis. The leaf runner owns
    the publication lock during execution; task transitions acquire it separately.
    """
    directory.mkdir(parents=True, exist_ok=True)
    task: dict[str, Any] = {"status": "running", "exit_code": None, "source": "."}
    with publication_lock(directory):
        previous = completed_measurement(directory)
        if previous is not None:
            logger.info("Reusing completed measurement: %s", task_source(directory))
            return BenchmarkRun(previous["record"], previous["metrics"], None, {}, exit_code=previous["exit_code"])
        write_records(directory, {"task.json": task})
    try:
        run = execute()
    except BaseException as error:
        interrupted = isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
        task.update(
            status="interrupted" if interrupted else "failed",
            exit_code=130 if interrupted else 1, error_type=type(error).__name__,
        )
        try:
            with publication_lock(directory):
                write_records(directory, {"task.json": task})
        except (OSError, sqlite3.Error):
            logger.exception("Could not retain the task outcome")
        raise
    with publication_lock(directory):
        documents = {"task.json": {**task, "status": "completed", "exit_code": run.exit_code}}
        # Measured leaves already own their committed result and publication.
        # Dry runs have no leaf recorder, so the task retains their returned result.
        if not read_records(directory, ("publication.json",)):
            documents.update({
                "config.json": run.record, "metrics.json": run.metrics,
                "publication.json": {
                    "record": run.record, "exit_code": run.exit_code,
                    "has_measurements": False, "http_series_published": False, "artifacts": {},
                },
            })
        write_records(directory, documents)
    return run


def task_source(directory: Path) -> Path:
    """Resolve a reused task's original artifacts for result readers and later resumptions."""
    task = read_records(directory, ("task.json",)).get("task.json")
    if task is None or task["status"] != "completed":
        return directory
    return (directory / task["source"]).resolve()


def completed_measurement(directory: Path) -> dict[str, Any] | None:
    """Read a committed measurement independently of its task's later output or cleanup failure."""
    task = read_records(directory, ("task.json",)).get("task.json")
    if task is None:
        return None
    origin = (directory / task["source"]).resolve() if task["status"] == "completed" else directory
    documents = read_records(origin, ("context.json", "config.json", "publication.json", "metrics.json", "task.json"))
    context = documents.get("context.json", {})
    publication = documents.get("publication.json")
    if publication is not None and "metrics.json" in documents and (
        context.get("measurement_exit_code") is not None or task["status"] == "completed"
    ):
        return {
            **task, "status": "completed", "exit_code": publication["exit_code"],
            "record": publication["record"], "metrics": documents["metrics.json"],
        }
    if (origin / DATABASE_FILENAME).is_file():
        return None
    # Earlier task files embedded their result. Incomplete legacy measurements
    # may instead have a completed context and separate config/metrics exports.
    legacy = documents.get("task.json", {})
    if "record" in legacy and "metrics" in legacy and legacy["status"] == "completed":
        return {**task, "status": "completed", "exit_code": legacy["exit_code"],
                "record": legacy["record"], "metrics": legacy["metrics"]}
    if context.get("measurement_exit_code") is not None and "config.json" in documents and "metrics.json" in documents:
        return {
            **task, "status": "completed", "exit_code": context["measurement_exit_code"],
            "record": documents["config.json"], "metrics": documents["metrics.json"],
        }
    return None
