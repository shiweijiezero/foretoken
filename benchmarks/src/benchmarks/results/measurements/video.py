# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregate measured video timings without owning transport or result destinations."""

from __future__ import annotations

import statistics
from typing import Any

from benchmarks.integrations.video.types import VideoSampleResult

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
        "failed_num": len(results) - len(successful),
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
