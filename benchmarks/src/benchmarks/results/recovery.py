# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Coordinate saved-output recovery and task continuation without repeating measurements."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import wandb

from benchmarks.config.benchmark import BenchmarkOutputConfig
from benchmarks.results.plots.export import render_results
from benchmarks.results.recording.directories import result_sources
from benchmarks.results.recording.files import write_json
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.recovery import (
    measurement_complete,
    publication_inputs,
    restore_run,
)
from benchmarks.results.recording.tasks import completed_measurement, task_source
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.publication import publish_saved_wandb, sync_publication

logger = logging.getLogger(__name__)


def recover_results(directory: Path, outputs: BenchmarkOutputConfig) -> BenchmarkRun:
    """Finish completed children before their aggregate, retaining each original measurement and run."""
    directory = directory.resolve()
    for source in result_sources(directory):
        if source == directory:
            continue
        context = json.loads((source / "context.json").read_text())
        if measurement_complete(source, context):
            recover_publication(source, outputs.for_child_run())
        elif (
            outputs.includes("wandb") and wandb.setup().settings.mode not in {"offline", "dryrun", "disabled"}
            and ((source / "wandb.json").is_file() or any((source / "wandb").glob("*/run-*.wandb")))
        ):
            sync_publication(source)
    return recover_publication(directory, outputs)


def recover_publication(directory: Path, outputs: BenchmarkOutputConfig) -> BenchmarkRun:
    """Own saved-output finalization after any active recorder releases its measurement."""
    directory = directory.resolve()
    with publication_lock(directory):
        context = json.loads((directory / "context.json").read_text())
        if not measurement_complete(directory, context):
            raise ValueError(f"No completed measurement to recover in {directory}")
        publication = publication_inputs(directory, context)
        run = restore_run(directory, publication)
        if outputs.includes("plot"):
            render_results(directory)
            run.artifacts["plots"] = directory / "plots"
        publication["artifacts"] = {
            name: Path(os.path.relpath(path, directory)).as_posix() for name, path in run.artifacts.items()
        }
        mode = wandb.setup().settings.mode if outputs.includes("wandb") else "disabled"
        if mode not in {"offline", "dryrun", "disabled"}:
            publish_saved_wandb(directory, run, publication)
        else:
            publication.update(
                publication_pending=mode in {"offline", "dryrun"}, recovered_at=datetime.now(UTC).isoformat(),
            )
            write_json(str(directory), "publication.json", publication)
        logger.info("Recovered saved results without measurement: %s", directory)
        return run


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
        task = completed_measurement(path.parent)
        if task is None:
            continue
        origin = task_source(path.parent)
        if not origin.is_dir():
            raise ValueError(f"Original task artifacts are missing: {origin}")
        if (origin / "context.json").is_file():
            recover_publication(origin, outputs.for_child_run())
        target = destination / path.parent.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        write_json(str(target), "task.json", {
            **task, "source": os.path.relpath(origin, target),
        })
