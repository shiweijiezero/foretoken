# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregate per-request measurements into benchmark metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

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
    input_tokens: int
    output_tokens: int
    succeeded: bool
    conversation_id: str | None
    turn: int | None


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
    ttft: Optional[float],
    output_tokens: int,
) -> Optional[float]:
    """Compute TPOT from the latency and token count of one streamed request."""
    if ttft is None:
        return None
    denominator = int(output_tokens) - 1
    if denominator <= 0:
        return None
    return (latency - ttft) / denominator


def configured_user_denominator(parallel: int) -> int:
    """Return the denominator for per-user throughput; unbounded open-loop uses one."""
    return 1 if parallel < 0 else int(parallel)


def generation_tokens_per_second_per_user(
    generation_tokens_per_second: float,
    parallel: int,
) -> float:
    """Normalize output throughput by the configured closed-loop concurrency."""
    return float(generation_tokens_per_second) / float(
        configured_user_denominator(parallel)
    )


def generation_tokens_per_second_per_gpu(
    generation_tokens_per_second: float,
    gpu_count: int,
) -> float:
    """Normalize output throughput by the GPU count for the current workload point."""
    if gpu_count < 1:
        raise ValueError(f"gpu_count must be >= 1, got {gpu_count}")
    return float(generation_tokens_per_second) / float(gpu_count)


def summarize_measurements(
    measurements: list[RequestMeasurement],
    *,
    total_time: float,
    stream: bool,
    arrival_rate: float,
    request_count: int,
    reported_concurrency: int,
    include_user_throughput: bool = True,
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

    output_tokens = sum(item.output_tokens for item in successful)
    input_tokens = sum(item.input_tokens for item in successful)
    success_count = len(successful)
    request_num = len(measurements)

    throughput: dict[str, Any] = {
        "requests_per_second": success_count / total_time,
        "generation_tokens_per_second": output_tokens / total_time,
        "prompt_tokens_per_second": input_tokens / total_time,
        "total_tokens_per_second": (input_tokens + output_tokens) / total_time,
    }
    if include_user_throughput:
        throughput["generation_tokens_per_second_per_user"] = (
            generation_tokens_per_second_per_user(
                throughput["generation_tokens_per_second"],
                reported_concurrency,
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
            input_tokens / success_count if success_count else None
        ),
        "avg_output_tokens": (
            output_tokens / success_count if success_count else None
        ),
        "benchmark_time": total_time,
        "rate": arrival_rate,
        "number": request_count,
        "parallel": reported_concurrency,
    }
