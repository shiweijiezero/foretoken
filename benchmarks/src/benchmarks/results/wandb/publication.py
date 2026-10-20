# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish saved results without acquiring a model service or repeating measurements."""

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

from benchmarks.results.recording.directories import figure_source
from benchmarks.results.recording.files import write_json
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.recovery import (
    measurement_complete,
    publication_inputs,
    restore_run,
)
from benchmarks.results.types import BenchmarkRun

logger = logging.getLogger(__name__)


def sync_results(sources: list[Path], *, figures: Path, figure_owner: Path | None) -> None:
    """Publish resolved runs independently, attaching a plot export only to its recorded owner."""
    recordings = [source for source in sources
                  if (source / "wandb.json").is_file() or any((source / "wandb").glob("*/run-*.wandb"))]
    if not recordings:
        raise ValueError("No W&B recordings found in the selected results")
    failures = []
    for source in recordings:
        try:
            sync_publication(source, plots=figures if source == figure_owner else None, exclude_plots=figures)
        except (OSError, ValueError, RuntimeError, wandb.errors.Error) as error:
            logger.error("W&B publication failed for %s: %s", source, error)
            failures.append(error)
    if failures:
        raise RuntimeError(f"W&B publication failed for {len(failures)} run(s)") from failures[0]


def sync_publication(
    directory: Path, *, native_synced: bool = False,
    plots: Path | None = None, exclude_plots: Path | None = None,
) -> None:
    """Publish a sealed recording, including diagnostics from an interrupted measurement."""
    directory = directory.resolve()
    with publication_lock(directory):
        context = json.loads((directory / "context.json").read_text())
        publication = None
        run = None
        try:
            saved_inputs = (directory / "publication.json").is_file() or (directory / "metrics.json").is_file()
            if measurement_complete(directory, context) and saved_inputs:
                publication = publication_inputs(directory, context)
                run = restore_run(directory, publication)
            elif context["status"] == "completed":
                raise ValueError(f"Completed results are missing metrics.json in {directory}")
        except (OSError, ValueError) as error:
            try:
                publish_saved_wandb(directory, None, None, native_synced=native_synced)
            except (OSError, ValueError, RuntimeError, wandb.errors.Error):
                logger.exception("W&B diagnostic publication also failed")
            _record_publication_error(directory, error, publication)
            raise
        if run is not None:
            if exclude_plots is not None and run.artifacts.get("plots") == exclude_plots:
                run.artifacts.pop("plots")
            if plots is not None:
                run.artifacts["plots"] = plots
                if plots.is_relative_to(directory):
                    publication["artifacts"]["plots"] = plots.relative_to(directory).as_posix()
        publish_saved_wandb(directory, run, publication, native_synced=native_synced, refresh_plots=plots is not None)


def publish_saved_wandb(
    directory: Path, run: BenchmarkRun | None, publication: dict[str, Any] | None,
    *, native_synced: bool = False, refresh_plots: bool = False,
) -> None:
    """Publish saved W&B outputs and retain their outcome while the caller owns the result lock."""
    if publication is not None:
        publication["publication_pending"] = True
        write_json(str(directory), "publication.json", publication)
    try:
        _upload_wandb(directory, run, native_synced=native_synced, refresh_plots=refresh_plots)
    except (OSError, ValueError, RuntimeError, wandb.errors.Error) as error:
        _record_publication_error(directory, error, publication)
        raise
    if publication is not None:
        publication.update(publication_pending=False, recovered_at=datetime.now(UTC).isoformat())
        publication.pop("publication_error", None)
        write_json(str(directory), "publication.json", publication)


def _publishers() -> dict[str, Callable[[Any, BenchmarkRun], None]]:
    """Map saved result kinds to their existing destination adapters explicitly."""
    from benchmarks.results.wandb.comparisons import (
        publish_distribution_comparison_wandb,
        publish_greedy_comparison_wandb,
    )
    from benchmarks.results.wandb.evaluation import (
        publish_evaluation_comparison_wandb,
        publish_quality_wandb,
    )
    from benchmarks.results.wandb.http import (
        publish_http_wandb,
        publish_slo_wandb,
        publish_sweep_wandb,
    )
    from benchmarks.results.wandb.video import publish_video_wandb

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


def _wandb_identity(directory: Path) -> dict[str, Any]:
    """Read the original destination, deriving identity only for a unique legacy recording."""
    path = directory / "wandb.json"
    if path.is_file():
        return json.loads(path.read_text())
    config = json.loads((directory / "config.json").read_text())
    destination = config.get("base", config)["wandb"]
    recordings = {path.resolve() for path in (directory / "wandb").glob("*/run-*.wandb")}
    if len(recordings) != 1:
        raise ValueError(f"Expected one original W&B recording in {directory}, found {len(recordings)}")
    recording = recordings.pop()
    publisher = {
        "http_load": "http", "arrival_trace": "http", "parameter_sweep": "sweep",
        "video_parameter_sweep": "sweep", "slo_search": "slo", "video_generation": "video",
        "evaluation": "evaluation", "evaluation_comparison": "evaluation_comparison",
        "distribution_comparison": "distribution_comparison", "greedy_comparison": "greedy_comparison",
    }[config.get("mode", "http_load")]
    identity = {
        "id": recording.stem.removeprefix("run-"), "project": destination["project"],
        "entity": destination.get("entity", ""), "base_url": wandb.setup().settings.base_url,
        "directory": Path(os.path.relpath(recording.parent, directory)).as_posix(), "publisher": publisher,
    }
    write_json(str(directory), "wandb.json", identity)
    return identity


def _diagnostic_paths(directory: Path) -> dict[str, list[str]]:
    """Select result-owned diagnostics independently of whether measurement completed."""
    responses = {"responses.jsonl", "warmup_responses.jsonl"}
    groups = {
        "logs": ["run.log", "context.json", "evaluator.log"],
        "records": [path.name for path in sorted(directory.glob("*.jsonl")) if path.name not in responses],
        "responses": sorted(responses),
        "warmup": ["warmup_metrics.json", "warmup_raw_output.json"],
    }
    if not (directory / "publication.json").is_file():
        groups["diagnostics"] = ["config.json", "metrics.json", "environment.json", "raw_output.json", "raw_results.json"]
        if (directory / "native").is_dir():
            groups["native"] = ["native"]
    return {kind: [name for name in names if (directory / name).exists()] for kind, names in groups.items()}


def record_wandb_diagnostics(directory: Path) -> None:
    """Retain the exact diagnostic file selection before the recorder releases its result lock."""
    identity = _wandb_identity(directory)
    identity["diagnostics"] = _diagnostic_paths(directory)
    write_json(str(directory), "wandb.json", identity)


def _publish_artifacts(
    session: Any, directory: Path, run: BenchmarkRun | None, identity: dict[str, Any],
    *, plots_only: bool = False,
) -> Any | None:
    """Upload owned result files and diagnostics through one artifact lifecycle."""
    primary = None
    if run is not None and not plots_only:
        primary = wandb.Artifact(f"results-{session.id}", type="benchmark-results")
        for name, path in run.artifacts.items():
            if name == "plots" or name.startswith("warmup_"):
                continue
            if path.is_dir():
                primary.add_dir(str(path), name=name)
            else:
                primary.add_file(str(path), name=path.name)
        primary = session.log_artifact(primary)
        primary.wait()
    diagnostics = {} if plots_only else identity.get("diagnostics")
    if diagnostics is None:
        diagnostics = _diagnostic_paths(directory)
    groups = {kind: [directory / name for name in names] for kind, names in diagnostics.items()}
    if run is not None and "plots" in run.artifacts:
        groups["plots"] = [run.artifacts["plots"]]
    for kind, paths in groups.items():
        if not paths:
            continue
        for path in paths:
            if not path.exists():
                raise ValueError(f"Recorded {kind} artifact is missing: {path}")
        artifact = wandb.Artifact(f"{kind}-{session.id}", type=f"benchmark-{kind}")
        for path in paths:
            if kind == "plots":
                with publication_lock(path, scope="plots"):
                    if figure_source(path) != directory:
                        raise ValueError(f"Figure ownership changed before publication: {path}; redraw the selected results")
                    files = json.loads((path / ".foretoken-plots.json").read_text())["files"]
                    for name in files:
                        if Path(name).name != name:
                            raise ValueError(f"Invalid figure filename: {name}")
                        artifact.add_file(str(path / name), name=name)
            elif path.is_dir():
                artifact.add_dir(str(path))
            else:
                artifact.add_file(str(path), name=path.name)
        session.log_artifact(artifact).wait()
    return primary


def _record_publication_error(
    directory: Path, error: BaseException, publication: dict[str, Any] | None = None,
) -> None:
    """Retain a publication failure without replacing it with an error saving diagnostics."""
    try:
        if publication is not None:
            publication.update(publication_pending=True, publication_error=str(error))
            write_json(str(directory), "publication.json", publication)
        elif (directory / "wandb.json").is_file():
            identity = json.loads((directory / "wandb.json").read_text())
            identity["publication_error"] = str(error)
            write_json(str(directory), "wandb.json", identity)
    except (OSError, ValueError):
        logger.exception("Could not retain the W&B publication error")


def _upload_wandb(
    directory: Path, run: BenchmarkRun | None, *, native_synced: bool = False, refresh_plots: bool = False,
) -> None:
    """Finish the original run under its result lock, without replaying its live measurement history."""
    identity = _wandb_identity(directory)
    plots = run is not None and "plots" in run.artifacts
    published = identity.get("published_scope") or ("results" if identity.get("recovered") else None)
    results_published = published == "results"
    plots_pending = plots and (refresh_plots or not identity.get("plots_published"))
    if published and (run is None or results_published) and not plots_pending:
        return
    settings = wandb.setup().settings
    if identity["base_url"].rstrip("/") != settings.base_url.rstrip("/"):
        raise ValueError(
            f"Saved results use W&B server {identity['base_url']}; configure WANDB_BASE_URL for that server before publishing"
        )
    native = (directory / identity["directory"]).resolve()
    with publication_lock(native):
        api = wandb.Api(overrides={"base_url": identity["base_url"]})
        entity = identity["entity"] or api.default_entity
        identity["entity"] = entity
        write_json(str(directory), "wandb.json", identity)
        if not native_synced and not published:
            logger.info("Synchronizing W&B recording: %s", directory)
            environment = os.environ.copy()
            environment.pop("WANDB_SERVICE", None)
            environment["WANDB_BASE_URL"] = identity["base_url"]
            environment["WANDB_MODE"] = "online"
            result = subprocess.run(
                [sys.executable, "-m", "wandb", "sync", "--include-synced", "--entity", entity,
                 "--project", identity["project"], "--id", identity["id"], str(native)],
                env=environment, check=False,
            )
            if result.returncode:
                raise RuntimeError(f"W&B synchronization failed for {directory}; saved results remain available for retry")
        scope = "results" if run is not None else "diagnostics"
        logger.info("Publishing W&B %s: %s/%s/%s", scope, entity, identity["project"], identity["id"])
        context = json.loads((directory / "context.json").read_text())
        exit_code = run.exit_code if run is not None else context.get("exit_code")
        if exit_code is None:
            exit_code = 1
        session = wandb.init(
            entity=entity, project=identity["project"], id=identity["id"],
            resume="must", mode="online", reinit="create_new", dir=str(directory),
            save_code=False,
            # The uploader must not replace the measurement's Git or environment files.
            settings=wandb.Settings(
                base_url=identity["base_url"], disable_git=True,
                x_disable_stats=True, x_disable_meta=True,
            ),
        )
        if session is None:
            raise RuntimeError("W&B did not resume the saved run")
        try:
            # Redrawing an already published run updates its figures, not its measured history.
            if run is not None and not results_published:
                _publishers()[identity["publisher"]](session, run)
                session.summary.update({"Foretoken/measurement_exit_code": run.exit_code})
            primary = _publish_artifacts(session, directory, run, identity, plots_only=results_published)
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                session.finish(exit_code=exit_code or 0)
            except (OSError, wandb.errors.Error):
                if not failed:
                    raise
                logger.exception("W&B publication cleanup also failed")
        if primary is not None:
            remote = api.run(f"{entity}/{identity['project']}/{identity['id']}")
            if remote.summary.get("Foretoken/measurement_exit_code") != run.exit_code:
                raise RuntimeError("W&B final result was not confirmed; repeat sync with the saved result directory")
            confirmed = api.artifact(primary.qualified_name)
            if "metrics.json" not in confirmed.manifest.entries:
                raise RuntimeError("W&B result artifacts were not confirmed; repeat sync with the saved result directory")
        identity.update(published_scope=scope, plots_published=plots)
        identity.pop("recovered", None)
        identity.pop("publication_error", None)
        write_json(str(directory), "wandb.json", identity)
