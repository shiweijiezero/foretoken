# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Aggregate per-request results into summary metrics."""

from __future__ import annotations

from typing import Any, Optional

import numpy as np


def _percentile_stats(values: list[float]) -> dict[str, float]:
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


def tokens_per_s_per_user(tokens_per_second: float, parallel: int) -> float:
    return float(tokens_per_second) / float(user_count_for_throughput(parallel))


def attach_user_throughput(
    metrics: dict[str, Any],
    *,
    parallel: int,
) -> dict[str, Any]:
    metrics["parallel"] = int(parallel)
    throughput = metrics["throughput"]
    tokens_per_second = float(throughput["token/s"])
    throughput["token/s/user"] = tokens_per_s_per_user(
        tokens_per_second, parallel
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

        latencies = [float(result["latency"]) for result in success_results]
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
            "success_rate": success_count / len(results),
            "latency": _percentile_stats(latencies),
            "ttft": _percentile_stats(ttfts),
            "tpot": _percentile_stats(tpots),
            "itl": _percentile_stats(tpots),
            "throughput": {
                "request/s": len(results) / total_time,
                "token/s": output_tokens / total_time,
                "input_token/s": input_tokens / total_time,
                "total_token/s": (input_tokens + output_tokens) / total_time,
            },
            "avg_input_tokens": input_tokens / success_count,
            "avg_output_tokens": output_tokens / success_count,
            "benchmark_time": total_time,
        }
