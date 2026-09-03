# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Aggregate per-request results into summary metrics."""

from __future__ import annotations

from typing import Any, Optional

import numpy as np


def percentile_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }
    array = np.asarray(values, dtype=float)
    return {
        "mean": float(np.mean(array)),
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "p99": float(np.percentile(array, 99)),
    }


def compute_tpot(
    latency: float,
    ttft: Optional[float],
    output_tokens: int,
) -> Optional[float]:
    if ttft is None:
        return None
    denominator = int(output_tokens) - 1
    if denominator <= 0:
        return None
    return (latency - ttft) / denominator


def user_count_for_throughput(parallel: int) -> int:
    """Denominator for per-user throughput (open-loop parallel < 0 → 1)."""
    return 1 if parallel < 0 else int(parallel)


def generation_tokens_per_second_per_user(
    generation_tokens_per_second: float,
    parallel: int,
) -> float:
    return float(generation_tokens_per_second) / float(
        user_count_for_throughput(parallel)
    )


def generation_tokens_per_second_per_gpu(
    generation_tokens_per_second: float,
    gpu_count: int,
) -> float:
    """Normalize token throughput by the GPUs represented by one point."""
    if gpu_count < 1:
        raise ValueError(f"gpu_count must be >= 1, got {gpu_count}")
    return float(generation_tokens_per_second) / float(gpu_count)


def attach_user_throughput(
    metrics: dict[str, Any],
    *,
    parallel: int,
) -> dict[str, Any]:
    metrics["parallel"] = int(parallel)
    throughput = metrics["throughput"]
    generation_tokens_per_second = float(throughput["generation_tokens_per_second"])
    throughput[
        "generation_tokens_per_second_per_user"
    ] = generation_tokens_per_second_per_user(
        generation_tokens_per_second,
        parallel,
    )
    return metrics


def merge_raw_outputs(raw_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Concatenate per-dataset raw outputs; ``total_time`` is the sum of walls."""
    if not raw_outputs:
        raise ValueError("merge_raw_outputs requires at least one raw output")
    results: list[Any] = []
    total_time = 0.0
    for raw_output in raw_outputs:
        results.extend(raw_output["results"])
        total_time += float(raw_output["total_time"])
    return {"results": results, "total_time": total_time}


class MetricsAggregator:
    def aggregate(self, output: dict[str, Any]) -> dict[str, Any]:
        results = output["results"]
        success_results = [result for result in results if result["success"]]

        stream_modes = {bool(result["stream"]) for result in results}
        if not results:
            streamed = True
        elif len(stream_modes) != 1:
            raise ValueError(
                f"mixed stream modes in one run: {sorted(stream_modes)}"
            )
        else:
            streamed = stream_modes.pop()

        latencies = [float(result["latency"]) for result in success_results]
        if streamed:
            ttfts = [
                float(result["ttft"])
                for result in success_results
                if result["ttft"] is not None
            ]
            tpots = [
                float(result["tpot"])
                for result in success_results
                if result["tpot"] is not None
            ]
        else:
            ttfts = []
            tpots = []

        output_tokens = sum(
            int(result["output_tokens"]) for result in success_results
        )
        input_tokens = sum(
            int(result["input_tokens"]) for result in success_results
        )
        total_time = float(output["total_time"])
        success_count = len(success_results)
        failed_count = len(results) - success_count

        return {
            "request_num": len(results),
            "success_num": success_count,
            "failed_num": failed_count,
            "success_rate": success_count / len(results) if len(results) else 0.0,
            "stream": streamed,
            "latency": percentile_stats(latencies),
            "ttft": percentile_stats(ttfts),
            "tpot": percentile_stats(tpots),
            "itl": percentile_stats(tpots),
            "throughput": {
                "requests_per_second": len(results) / total_time,
                "generation_tokens_per_second": output_tokens / total_time,
                "prompt_tokens_per_second": input_tokens / total_time,
                "total_tokens_per_second": (input_tokens + output_tokens) / total_time,
            },
            "avg_input_tokens": (
                input_tokens / success_count if success_count else None
            ),
            "avg_output_tokens": (
                output_tokens / success_count if success_count else None
            ),
            "benchmark_time": total_time,
        }
