# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregation and console output for video-generation benchmarks."""

from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoSampleResult
from benchmarks.results.output import (
    BenchmarkRun,
    ResultSink,
    write_json,
)
from benchmarks.results.video_wandb import VideoWandbSink

logger = logging.getLogger(__name__)

_TIMING_FIELDS = (
    "e2e_s",
    "queue_wait_s",
    "server_generation_s",
    "preprocess_s",
    "encode_s",
    "media_encode_s",
    "denoise_s",
    "decode_s",
    "postprocess_s",
    "denoise_per_step_s",
)


def aggregate_video_results(results: list[VideoSampleResult]) -> dict[str, Any]:
    """Aggregate successful video requests into stable summary metrics."""
    successful = [item for item in results if item.success]
    metrics: dict[str, Any] = {
        "request_num": len(results),
        "success_num": len(successful),
        "success_rate": len(successful) / len(results) if results else 0.0,
    }
    for name in _TIMING_FIELDS:
        values = [
            float(value)
            for item in successful
            if (value := getattr(item, name)) is not None
        ]
        metrics[name] = statistics.fmean(values) if values else None
    memory = [
        float(item.peak_gpu_memory_mb)
        for item in successful
        if item.peak_gpu_memory_mb is not None
    ]
    metrics["peak_gpu_memory_mb"] = max(memory) if memory else None
    metrics["peak_gpu_memory_source"] = (
        "response_header" if memory else "unavailable"
    )
    tokens = [
        item.reference_tokens
        for item in successful
        if item.reference_tokens is not None
    ]
    metrics["reference_tokens"] = max(tokens) if tokens else None
    metrics["reference_tokens_source"] = (
        next(
            item.reference_tokens_source
            for item in successful
            if item.reference_tokens is not None
        )
        if tokens
        else "unavailable"
    )
    metrics["output_bytes"] = sum(item.output_bytes for item in successful)
    return metrics


def _seconds(value: Any) -> str:
    seconds = float(value)
    return f"{seconds * 1000:.2f} ms" if seconds < 0.01 else f"{seconds:.2f} s"


def log_video_summary(
    config: VideoBenchmarkConfig, metrics: dict[str, Any]
) -> None:
    """Log a compact summary for the completed video dataset run."""
    def shared(name: str) -> str:
        values = {getattr(request, name) for request in config.requests}
        return str(next(iter(values))) if len(values) == 1 else "mixed"

    resolutions = {
        (request.width, request.height) for request in config.requests
    }
    resolution = "mixed"
    if len(resolutions) == 1:
        width, height = next(iter(resolutions))
        resolution = f"{width}x{height}"
    lines = [
        "======== Foretoken Video Generation Benchmark ========",
        f"  Workload          {shared('task').upper()}",
        f"  Resolution        {resolution}",
        f"  Frames            {shared('num_frames')}",
        f"  Steps             {shared('num_inference_steps')}",
        f"  Success           {metrics['success_num']}/{metrics['request_num']}",
        "",
    ]
    for label, field in (
        ("E2E", "e2e_s"),
        ("Queue wait", "queue_wait_s"),
        ("Server generation", "server_generation_s"),
        ("Preprocess", "preprocess_s"),
        ("Text encode", "encode_s"),
        ("Media encode", "media_encode_s"),
        ("Denoise", "denoise_s"),
        ("Decode", "decode_s"),
        ("Postprocess", "postprocess_s"),
        ("Denoise / step", "denoise_per_step_s"),
    ):
        if (value := metrics.get(field)) is not None:
            lines.append(f"  {label:<18}{_seconds(value)}")
    if (peak := metrics.get("peak_gpu_memory_mb")) is not None:
        lines.append(f"  {'Peak GPU memory':<18}{float(peak) / 1024:.2f} GB")
    if (tokens := metrics.get("reference_tokens")) is not None:
        source = metrics.get("reference_tokens_source")
        lines.append(f"  {'Reference tokens':<18}{tokens} ({source})")
    lines.append("=====================================================")
    logger.info("\n%s", "\n".join(lines))


def video_run_record(config: VideoBenchmarkConfig) -> dict[str, Any]:
    """Build the shared result record for one video benchmark run."""
    return {
        "mode": "video_generation",
        "dataset": config.dataset_source,
        "parallel": config.concurrency,
        "number": len(config.requests),
    }


def create_video_benchmark_run(
    record: dict[str, Any],
    results: list[VideoSampleResult],
    run_dir: Path,
) -> BenchmarkRun:
    """Aggregate video results and materialize their per-request artifact."""
    metrics = aggregate_video_results(results)
    raw_results = write_json(
        str(run_dir),
        "raw_results.json",
        [item.to_dict() for item in results],
    )
    return BenchmarkRun(
        record=record,
        metrics=metrics,
        measurements=None,
        artifacts={"raw_results": raw_results},
    )


class VideoArtifactSink:
    """Materialize video configuration and summary files in the execution directory."""

    def __init__(self, config: VideoBenchmarkConfig, run_dir: Path) -> None:
        self.config = config
        self.run_dir = run_dir
        self.config_path: Path | None = None

    def open(self, record: dict[str, Any]) -> None:
        """Write configuration before requests so failed runs remain reproducible."""
        self.config_path = write_json(
            str(self.run_dir), "config.json", self.config.to_dict()
        )

    def publish(self, run: BenchmarkRun) -> None:
        """Complete the shared artifact map with video summary metadata."""
        if self.config_path is None:
            raise RuntimeError("video artifact sink is not open")
        run.artifacts["config"] = self.config_path
        run.artifacts["metrics"] = write_json(
            str(self.run_dir), "metrics.json", run.metrics
        )

    def close(self) -> None:
        """Release no resources because ResultOutputs owns the directory."""
        return None


class VideoConsoleSink:
    """Publish the video-specific aggregate summary to the console."""

    def __init__(self, config: VideoBenchmarkConfig) -> None:
        self.config = config

    def open(self, record: dict[str, Any]) -> None:
        """Acquire no resources before console publication."""
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Log the completed video summary."""
        log_video_summary(self.config, run.metrics)

    def close(self) -> None:
        """Release no resources after console publication."""
        return None


class VideoLocalSink:
    """Report the persistent directory selected by ResultOutputs."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def open(self, record: dict[str, Any]) -> None:
        """Acquire no resources because ResultOutputs created the directory."""
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Report where the completed video artifacts were retained."""
        logger.info("Video artifacts: %s", self.run_dir)

    def close(self) -> None:
        """Release no resources because ResultOutputs owns the directory."""
        return None


def video_result_sinks(
    config: VideoBenchmarkConfig, execution_dir: str
) -> list[ResultSink]:
    """Build video adapters for destinations managed by ResultOutputs."""
    run_dir = Path(execution_dir)
    sinks: list[ResultSink] = [VideoArtifactSink(config, run_dir)]
    if not config.outputs.includes("quiet"):
        sinks.append(VideoConsoleSink(config))
    if config.outputs.includes("local"):
        sinks.append(VideoLocalSink(run_dir))
    if config.outputs.includes("wandb"):
        sinks.append(VideoWandbSink(config, run_dir))
    return sinks
