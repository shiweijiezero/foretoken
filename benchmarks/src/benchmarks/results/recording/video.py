# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serialize video workload identity and completed media measurements for recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video.types import VideoSampleResult
from benchmarks.results.measurements.video import aggregate_video_results
from benchmarks.results.types import BenchmarkRun


def video_run_record(config: VideoBenchmarkConfig) -> dict[str, Any]:
    """Build the shared result record for one video benchmark run."""
    return {
        "mode": "video_generation",
        "name": config.name,
        "dataset": config.dataset_source,
        "max_concurrency": config.concurrency,
        "num_prompts": len(config.requests),
        "warmup_requests": config.warmup_requests,
        "duration": config.duration_s,
    }


def video_result_record(result: VideoSampleResult, directory: Path) -> dict[str, Any]:
    """Serialize a video completion with media paths relative to its owning result directory."""
    record = result.to_dict()
    if result.output_path:
        record["output_path"] = Path(result.output_path).relative_to(directory).as_posix()
    return record


def create_video_benchmark_run(
    record: dict[str, Any],
    results: list[VideoSampleResult],
    run_dir: Path,
) -> BenchmarkRun:
    """Aggregate completed video results; ResultOutputs commits them before exporting request records."""
    metrics = aggregate_video_results(results)
    return BenchmarkRun(
        record=record,
        metrics=metrics,
        measurements=None,
        artifacts={"raw_results": run_dir / "raw_results.json"},
        exit_code=0 if metrics["success_num"] else 1,
    )
