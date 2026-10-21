# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Rebuild measured runs from saved evidence independently of result destinations."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

from loguru import logger

from benchmarks.results.recording.database import (
    DATABASE_FILENAME,
    RunRecords,
    read_record,
    read_recorded_requests,
    read_records,
)
from benchmarks.results.recording.directories import figure_source
from benchmarks.results.recording.files import write_json
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.requests import (
    export_http_requests,
    load_http_measurements,
    read_request_records,
)
from benchmarks.results.types import BenchmarkRun


def restore_run(directory: Path, publication: dict[str, Any]) -> BenchmarkRun:
    """Rebuild a saved run for recovery or publication while the caller owns its result lock."""
    metrics = read_record(directory, "metrics.json")
    if (directory / DATABASE_FILENAME).is_file():
        with closing(RunRecords(directory)) as records:
            records.export()
    artifacts = {name: (directory / path).resolve() for name, path in publication["artifacts"].items() if name != "plots"}
    plots = publication["artifacts"].get("plots")
    if plots is not None:
        try:
            with publication_lock(directory / plots, scope="plots"):
                if figure_source(directory / plots) == directory:
                    artifacts["plots"] = (directory / plots).resolve()
        except (OSError, ValueError) as error:
            logger.warning("Cannot read saved figure ownership: {}", error)
    measurements = None
    if publication["has_measurements"]:
        raw = read_request_records(directory)
        if len(raw) != metrics["request_num"]:
            raise ValueError(f"Cannot recover completed measurement: request records are incomplete in {directory}")
        measurements = load_http_measurements(raw)
        artifacts["raw_output"] = export_http_requests(
            directory, metrics, traced=publication["record"].get("mode") == "arrival_trace",
        )
    elif publication["record"].get("mode") == "video_generation":
        raw = read_recorded_requests(directory, "raw_results")
        if len(raw) != metrics["request_num"]:
            raise ValueError(f"Cannot recover completed measurement: video records are incomplete in {directory}")
        artifacts["raw_results"] = write_json(str(directory), "raw_results.json", sorted(raw, key=lambda row: row["index"]))
    warmup = read_records(directory, ("warmup_metrics.json",)).get("warmup_metrics.json")
    if warmup is not None:
        if "stream" in warmup:
            export_http_requests(directory, warmup, name="warmup_raw_output")
        else:
            write_json(str(directory), "warmup_raw_output.json", sorted(
                read_recorded_requests(directory, "warmup_raw_output"), key=lambda row: row["index"],
            ))
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
        if path.exists():
            artifacts[name] = path
    for name, path in artifacts.items():
        if not path.exists():
            raise ValueError(f"Cannot publish saved {name}: {path} is missing")
    return BenchmarkRun(publication["record"], metrics, measurements, artifacts,
                        exit_code=publication["exit_code"],
                        http_series_published=publication["http_series_published"])


def measurement_complete(directory: Path, context: dict[str, Any]) -> bool:
    """Identify saved measurement completion independently of later output failures."""
    if (directory / DATABASE_FILENAME).is_file():
        documents = read_records(directory, ("config.json", "metrics.json", "publication.json"))
        return context.get("measurement_exit_code") is not None and len(documents) == 3
    return context.get("measurement_exit_code") is not None or (
        context.get("phase") == "finalization" and (directory / "metrics.json").is_file()
    )


def publication_inputs(directory: Path, context: dict[str, Any]) -> dict[str, Any]:
    """Read publication inputs under the caller's result lock, including older aggregate recordings."""
    documents = read_records(directory, ("publication.json", "config.json", "metrics.json"))
    if "publication.json" in documents:
        return documents["publication.json"]
    record = documents["config.json"]
    metrics = documents["metrics.json"]
    exit_code = context.get("measurement_exit_code")
    if exit_code is None:
        if "sweep" in metrics:
            exit_code = int(metrics["sweep"]["status"] != "completed")
        elif "slo_search" in metrics:
            exit_code = int(any(group["best_max_concurrency"] is None for group in metrics["slo_search"]["groups"]))
        else:
            raise ValueError(f"Saved results do not identify the completed measurement outcome: {directory}")
    return {
        "record": record, "exit_code": exit_code,
        "has_measurements": record.get("mode") in {"http_load", "arrival_trace"},
        "http_series_published": False, "artifacts": {},
    }
