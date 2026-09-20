# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregate per-request measurements into benchmark metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class RequestMeasurement:
    """Client-side observation of one model request, shared by every benchmark path.

    ``started_at`` is seconds from the start of the run to the request send.
    Streaming engines may provide TTFT, TPOT, and inter-token latency samples;
    non-streamed or failed requests leave them empty. ``conversation_id`` and
    ``turn`` identify the conversation and its request index when the path tracks
    them; EvalScope records leave both unset.
    """

    started_at: float
    ttft: float | None
    latency: float
    tpot: float | None
    itl_samples: tuple[float, ...]
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    succeeded: bool
    conversation_id: str | None
    turn: int | None
    status_code: int | None = None
    error_message: str | None = None


def percentile_summary(values: list[float]) -> dict[str, float | None]:
    """Compute mean and nearest-rank percentiles, matching EvalScope's estimator."""
    if not values:
        return {"mean": None, "p50": None, "p95": None, "p99": None}
    array = np.asarray(values, dtype=float)
    return {
        "mean": float(np.mean(array)),
        "p50": float(np.percentile(array, 50, method="inverted_cdf")),
        "p95": float(np.percentile(array, 95, method="inverted_cdf")),
        "p99": float(np.percentile(array, 99, method="inverted_cdf")),
    }


def compute_tpot(
    latency: float,
    ttft: float | None,
    output_tokens: int | None,
) -> float | None:
    """Compute TPOT from the latency and token count of one streamed request."""
    if ttft is None or output_tokens is None:
        return None
    denominator = int(output_tokens) - 1
    if denominator <= 0:
        return None
    return (latency - ttft) / denominator


def normalized_generation_throughput(
    generation_tokens_per_second: float | None,
    *,
    configured_concurrency: int,
    average_active_requests: float | None,
    gpu_count: int | None,
) -> dict[str, float | None]:
    """Normalize output throughput per user and per GPU.

    A finite ``--parallel`` value represents the configured user count. With
    unlimited concurrency, use the measured time-weighted average active
    requests instead. Return None when throughput or a denominator is missing.
    """
    per_user = None
    per_gpu = None
    if generation_tokens_per_second is not None:
        user_count = (
            float(configured_concurrency)
            if configured_concurrency > 0
            else average_active_requests
        )
        if user_count is not None and user_count > 0:
            per_user = float(generation_tokens_per_second) / user_count
        if gpu_count is not None:
            if gpu_count < 1:
                raise ValueError(f"gpu_count must be >= 1, got {gpu_count}")
            per_gpu = float(generation_tokens_per_second) / gpu_count
    return {
        "generation_tokens_per_second_per_user": per_user,
        "generation_tokens_per_second_per_gpu": per_gpu,
    }


def summarize_measurements(
    measurements: list[RequestMeasurement],
    *,
    total_time: float,
    stream: bool,
    arrival_rate: float,
    request_count: int,
    reported_concurrency: int,
    gpu_count: int | None,
    include_normalized_throughput: bool = True,
) -> dict[str, Any]:
    """Aggregate request measurements and workload coordinates into the published metrics.

    Latency distributions use successful requests only; throughput divides
    successful request and token counts by ``total_time``. TTFT and TPOT are
    reported only for streamed runs. Inter-token latency is included when the
    request engine records those samples.
    """
    successful = [item for item in measurements if item.succeeded]
    latencies = [item.latency for item in successful]
    ttfts: list[float] = []
    tpots: list[float] = []
    itls: list[float] = []
    if stream:
        ttfts = [item.ttft for item in successful if item.ttft is not None]
        tpots = [item.tpot for item in successful if item.tpot is not None]
        itls = [value for item in successful for value in item.itl_samples]

    input_complete = all(item.input_tokens is not None for item in successful)
    output_complete = all(item.output_tokens is not None for item in successful)
    input_tokens = (
        sum(int(item.input_tokens) for item in successful) if input_complete else None
    )
    output_tokens = (
        sum(int(item.output_tokens) for item in successful) if output_complete else None
    )
    reported_cached_tokens = [
        item.cached_input_tokens
        for item in successful
        if item.cached_input_tokens is not None
    ]
    success_count = len(successful)
    request_num = len(measurements)

    generation_tokens_per_second = (
        output_tokens / total_time if output_tokens is not None else None
    )
    prompt_tokens_per_second = (
        input_tokens / total_time if input_tokens is not None else None
    )
    total_tokens_per_second = (
        (input_tokens + output_tokens) / total_time
        if input_tokens is not None and output_tokens is not None
        else None
    )
    throughput: dict[str, Any] = {
        "requests_per_second": success_count / total_time,
        "generation_tokens_per_second": generation_tokens_per_second,
        "prompt_tokens_per_second": prompt_tokens_per_second,
        "total_tokens_per_second": total_tokens_per_second,
    }
    average_active_requests = (
        sum(item.latency for item in measurements) / total_time
        if measurements and total_time > 0
        else None
    )
    if include_normalized_throughput:
        throughput.update(
            normalized_generation_throughput(
                generation_tokens_per_second,
                configured_concurrency=reported_concurrency,
                average_active_requests=average_active_requests,
                gpu_count=gpu_count,
            )
        )
    return {
        "request_num": request_num,
        "success_num": success_count,
        "failed_num": request_num - success_count,
        "success_rate": success_count / request_num if request_num else 0.0,
        "stream": stream,
        "latency": percentile_summary(latencies),
        "ttft": percentile_summary(ttfts),
        "tpot": percentile_summary(tpots),
        "itl": percentile_summary(itls),
        "throughput": throughput,
        "avg_input_tokens": (
            input_tokens / success_count
            if success_count and input_tokens is not None
            else None
        ),
        "avg_output_tokens": (
            output_tokens / success_count
            if success_count and output_tokens is not None
            else None
        ),
        "avg_cached_input_tokens": (
            sum(reported_cached_tokens) / len(reported_cached_tokens)
            if reported_cached_tokens
            else None
        ),
        "benchmark_time": total_time,
        "rate": arrival_rate,
        "number": request_count,
        "parallel": reported_concurrency,
    }
