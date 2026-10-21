# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregation and console output for video-generation benchmarks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.results.reporting.local import LocalDirectorySink
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.recording import WandbSink
from benchmarks.results.wandb.video import publish_video_wandb

logger = logging.getLogger(__name__)


def video_result_sinks(
    config: VideoBenchmarkConfig, execution_dir: str
) -> list[ResultSink]:
    """Build video adapters for destinations managed by ResultOutputs."""
    run_dir = Path(execution_dir)
    sinks: list[ResultSink] = [VideoConsoleSink(config)]
    if config.outputs.saves_local:
        sinks.append(LocalDirectorySink(str(run_dir)))
    if config.outputs.includes("wandb"):
        sinks.append(
            WandbSink(
                config,
                execution_dir=str(run_dir),
                run_name=config.wandb.run_name.strip() or run_dir.name,
                group=config.wandb.group.strip(),
                publisher=publish_video_wandb,
                run_config={
                    **config.to_dict(),
                    "metric_scopes": {
                        "wandb_system": "benchmark_client",
                        "video_response": "remote_service",
                    },
                },
            )
        )
    return sinks


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


class VideoConsoleSink:
    """Publish the video-specific aggregate summary to the console."""

    def __init__(self, config: VideoBenchmarkConfig) -> None:
        self.config = config

    def open(self, record: dict[str, Any]) -> None:
        """Acquire no resources before console publication."""
        return

    def publish(self, run: BenchmarkRun) -> None:
        """Log the completed video summary."""
        log_video_summary(self.config, run.metrics)

    def close(self, *, exit_code: int = 0) -> None:
        """Release no resources after console publication."""
        return
