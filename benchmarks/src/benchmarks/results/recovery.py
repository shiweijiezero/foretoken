# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Finish saved result publication without acquiring a model service or repeating measurements."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import wandb

from benchmarks.config.benchmark import BenchmarkOutputConfig
from benchmarks.results.output import BenchmarkRun, write_json
from benchmarks.results.records import read_request_records
from benchmarks.results.wandb_sync import publication_lock

logger = logging.getLogger(__name__)


def _publishers() -> dict[str, Callable[[Any, BenchmarkRun], None]]:
    """Map saved result kinds to their existing destination adapters explicitly."""
    from benchmarks.results.distribution_comparison import (
        publish_distribution_comparison_wandb,
        publish_greedy_comparison_wandb,
    )
    from benchmarks.results.evaluation import (
        publish_evaluation_comparison_wandb,
        publish_quality_wandb,
    )
    from benchmarks.results.video_wandb import publish_video_wandb
    from benchmarks.results.wandb import (
        publish_http_wandb,
        publish_slo_wandb,
        publish_sweep_wandb,
    )

    return {
        "http": publish_http_wandb, "sweep": publish_sweep_wandb, "slo": publish_slo_wandb,
        "video": publish_video_wandb, "evaluation": publish_quality_wandb,
        "evaluation_comparison": publish_evaluation_comparison_wandb,
        "distribution_comparison": publish_distribution_comparison_wandb,
        "greedy_comparison": publish_greedy_comparison_wandb,
    }


def publisher_name(publisher: Callable[[Any, BenchmarkRun], None]) -> str:
    """Store a stable adapter identity alongside a run's W&B destination."""
    return next(name for name, candidate in _publishers().items() if candidate is publisher)


def _restore_run(directory: Path, publication: dict[str, Any]) -> BenchmarkRun:
    """Load the original measurement and materialize request output from its flushed journal."""
    from benchmarks.results.plots.measurements import load_http_measurements

    metrics = json.loads((directory / "metrics.json").read_text())
    artifacts = {name: (directory / path).resolve() for name, path in publication["artifacts"].items()}
    measurements = None
    if publication["has_measurements"]:
        raw = read_request_records(directory)
        if len(raw) != metrics["request_num"]:
            raise ValueError(f"Cannot recover completed measurement: request records are incomplete in {directory}")
        measurements = load_http_measurements(raw)
        if not (directory / "raw_output.json").is_file():
            write_json(str(directory), "raw_output.json", raw, compact=True)
        artifacts["raw_output"] = directory / "raw_output.json"
    for name, filename in (
        ("config", "config.json"), ("metrics", "metrics.json"), ("environment", "environment.json"),
        ("warmup_metrics", "warmup_metrics.json"), ("warmup_raw_output", "warmup_raw_output.json"),
        ("replica_observations", "replica_observations.json"), ("gpu_allocation", "gpu_allocation.json"),
        ("gpu_allocation_csv", "gpu_allocation.csv"), ("prometheus_observations", "prometheus_observations.json"),
        ("sweep_points", "sweep_points.json"), ("sweep_summary", "sweep_summary.json"),
        ("sweep_summary_csv", "sweep_summary.csv"), ("slo_results", "slo_results.json"),
        ("raw_results", "raw_results.json"),
    ):
        path = directory / filename
        if path.is_file():
            artifacts[name] = path
    for name, path in artifacts.items():
        if not path.exists():
            raise ValueError(f"Cannot recover saved {name}: {path} is missing")
    return BenchmarkRun(publication["record"], metrics, measurements, artifacts,
                        exit_code=publication["exit_code"],
                        http_series_published=publication["http_series_published"])


def _recover_wandb(directory: Path, run: BenchmarkRun) -> bool:
    """Synchronize original records, then finish their existing remote run through the public SDK."""
    identity_path = directory / "wandb.json"
    if not identity_path.is_file():
        config = json.loads((directory / "config.json").read_text())
        destination = config.get("base", config)["wandb"]
        recordings = {path.resolve() for path in (directory / "wandb").glob("*/run-*.wandb")}
        if len(recordings) != 1:
            raise ValueError(f"Expected one original W&B recording in {directory}, found {len(recordings)}")
        recording = recordings.pop()
        mode = config.get("mode", "http_load")
        publisher = {
            "http_load": "http", "arrival_trace": "http", "parameter_sweep": "sweep",
            "video_parameter_sweep": "sweep", "slo_search": "slo", "video_generation": "video",
            "evaluation": "evaluation", "evaluation_comparison": "evaluation_comparison",
            "distribution_comparison": "distribution_comparison", "greedy_comparison": "greedy_comparison",
        }[mode]
        identity = {
            "id": recording.stem.removeprefix("run-"), "project": destination["project"],
            "entity": destination.get("entity", ""), "base_url": wandb.setup().settings.base_url,
            "directory": os.path.relpath(recording.parent, directory), "publisher": publisher,
        }
        write_json(str(directory), "wandb.json", identity)
    identity = json.loads(identity_path.read_text())
    if identity.get("recovered") and ("plots" not in run.artifacts or identity.get("plots_published")):
        return True
    if wandb.setup().settings.mode in {"offline", "dryrun", "disabled"}:
        logger.info("W&B publication retained for online recovery: %s", directory)
        return False
    native = (directory / identity["directory"]).resolve()
    with publication_lock(native):
        api = wandb.Api(overrides={"base_url": identity["base_url"]})
        entity = identity["entity"] or api.default_entity
        identity["entity"] = entity
        write_json(str(directory), "wandb.json", identity)
        environment = os.environ.copy()
        environment.pop("WANDB_SERVICE", None)
        environment["WANDB_BASE_URL"] = identity["base_url"]
        if not identity.get("recovered"):
            result = subprocess.run(
                [sys.executable, "-m", "wandb", "sync", "--include-synced", "--entity", entity,
                 "--project", identity["project"], "--id", identity["id"], str(native)],
                env=environment, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"W&B synchronization failed for {directory}; saved results remain available for retry")
        session = wandb.init(
            entity=entity, project=identity["project"], id=identity["id"],
            resume="must", mode="online", reinit="create_new", dir=str(directory),
            settings=wandb.Settings(base_url=identity["base_url"], x_disable_stats=True),
        )
        if session is None:
            raise RuntimeError("W&B did not resume the saved run")
        try:
            _publishers()[identity["publisher"]](session, run)
            results = wandb.Artifact(f"results-{session.id}", type="benchmark-results")
            for name, path in run.artifacts.items():
                if name == "plots":
                    continue
                if path.is_dir():
                    results.add_dir(str(path), name=name)
                else:
                    results.add_file(str(path), name=path.name)
            uploaded_results = session.log_artifact(results)
            uploaded_results.wait()
            for kind, paths in (
                ("plots", [run.artifacts["plots"]] if "plots" in run.artifacts else []),
                ("logs", [directory / "run.log"]),
                ("records", sorted(directory.glob("*.jsonl"))),
                ("warmup", [run.artifacts[name] for name in ("warmup_metrics", "warmup_raw_output") if name in run.artifacts]),
            ):
                paths = [path for path in paths if path.exists()]
                if not paths:
                    continue
                artifact = wandb.Artifact(f"{kind}-{session.id}", type=f"benchmark-{kind}")
                for path in paths:
                    if path.is_dir():
                        artifact.add_dir(str(path))
                    else:
                        artifact.add_file(str(path), name=path.name)
                session.log_artifact(artifact).wait()
            session.summary.update({"Foretoken/measurement_exit_code": run.exit_code})
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                session.finish(exit_code=run.exit_code or 0)
            except (OSError, wandb.errors.Error):
                if not failed:
                    raise
                logger.exception("W&B recovery cleanup also failed")
        remote = api.run(f"{entity}/{identity['project']}/{identity['id']}")
        if remote.summary.get("Foretoken/measurement_exit_code") != run.exit_code:
            raise RuntimeError("W&B final result was not confirmed; repeat recovery with the saved result directory")
        confirmed = api.artifact(uploaded_results.qualified_name)
        if "metrics.json" not in confirmed.manifest.entries:
            raise RuntimeError("W&B result artifacts were not confirmed; repeat recovery with the saved result directory")
        identity.update(recovered=True, plots_published="plots" in run.artifacts)
        write_json(str(directory), "wandb.json", identity)
    return True


def recover_results(directory: Path, outputs: BenchmarkOutputConfig) -> BenchmarkRun:
    """Finish completed children before their saved aggregate, retaining each original W&B identity."""
    from benchmarks.results.tasks import task_source

    directory = directory.resolve()
    sources = {task_source(path.parent) for path in directory.rglob("task.json")}
    sources.update(path.parent for path in directory.rglob("publication.json"))
    for source in sorted(sources - {directory}, key=lambda path: len(path.parts), reverse=True):
        recover_publication(source, outputs.for_child_run())
    return recover_publication(directory, outputs)


def recover_publication(directory: Path, outputs: BenchmarkOutputConfig) -> BenchmarkRun:
    """Own offline finalization after any active recorder has released the original measurement."""
    directory = directory.resolve()
    with publication_lock(directory):
        return _recover_publication(directory, outputs)


def _recover_publication(directory: Path, outputs: BenchmarkOutputConfig) -> BenchmarkRun:
    """Rebuild presentation and remote publication without changing the original execution outcome."""
    context = json.loads((directory / "context.json").read_text())
    publication_path = directory / "publication.json"
    if context.get("measurement_exit_code") is None and not (
        context.get("phase") == "finalization" and (directory / "metrics.json").is_file()
    ):
        raise ValueError(f"No completed measurement to recover in {directory}")
    if publication_path.is_file():
        publication = json.loads(publication_path.read_text())
    else:
        record = json.loads((directory / "config.json").read_text())
        metrics = json.loads((directory / "metrics.json").read_text())
        exit_code = context.get("measurement_exit_code")
        if exit_code is None:
            if "sweep" in metrics:
                exit_code = int(metrics["sweep"]["status"] != "completed")
            elif "slo_search" in metrics:
                exit_code = int(any(group["best_max_concurrency"] is None for group in metrics["slo_search"]["groups"]))
            else:
                raise ValueError(f"Saved results do not identify the completed measurement outcome: {directory}")
        publication = {
            "record": record, "exit_code": exit_code,
            "has_measurements": record.get("mode") in {"http_load", "arrival_trace"},
            "http_series_published": False, "artifacts": {},
        }
    run = _restore_run(directory, publication)
    if outputs.includes("plot"):
        from benchmarks.results.plots import render_results

        render_results(directory)
        run.artifacts["plots"] = directory / "plots"
    pending = outputs.includes("wandb") and not _recover_wandb(directory, run)
    publication.update(
        publication_pending=pending, recovered_at=datetime.now(UTC).isoformat(),
        artifacts={name: os.path.relpath(path, directory) for name, path in run.artifacts.items()},
    )
    write_json(str(directory), "publication.json", publication)
    logger.info("Recovered saved results without measurement: %s", directory)
    return run
