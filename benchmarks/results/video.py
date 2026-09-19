# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregation and console output for video-generation benchmarks."""

from __future__ import annotations

import logging
import statistics
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoSampleResult

logger = logging.getLogger(__name__)

_TIMING_FIELDS = (
    "e2e_s",
    "queue_wait_s",
    "server_generation_s",
    "preprocess_s",
    "encode_s",
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
        ("Encode", "encode_s"),
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
