# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""聚合 HTTP 请求观测值并计算性能指标。"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np


def percentile_summary(values: list[float]) -> dict[str, float | None]:
    """计算性能汇总使用的均值和固定分位数。"""
    if not values:
        return {"mean": None, "p50": None, "p95": None, "p99": None}
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
    """根据一次流式请求的时延和 token 数计算 TPOT。"""
    if ttft is None:
        return None
    denominator = int(output_tokens) - 1
    if denominator <= 0:
        return None
    return (latency - ttft) / denominator


def configured_user_denominator(parallel: int) -> int:
    """返回每用户吞吐量分母；无限并发的 open-loop 使用一。"""
    return 1 if parallel < 0 else int(parallel)


def generation_tokens_per_second_per_user(
    generation_tokens_per_second: float,
    parallel: int,
) -> float:
    """按配置的 closed-loop 并发数归一化输出吞吐量。"""
    return float(generation_tokens_per_second) / float(
        configured_user_denominator(parallel)
    )


def generation_tokens_per_second_per_gpu(
    generation_tokens_per_second: float,
    gpu_count: int,
) -> float:
    """按当前负载点对应的 GPU 数归一化输出吞吐量。"""
    if gpu_count < 1:
        raise ValueError(f"gpu_count must be >= 1, got {gpu_count}")
    return float(generation_tokens_per_second) / float(gpu_count)


def attach_user_throughput(
    metrics: dict[str, Any],
    *,
    parallel: int,
) -> dict[str, Any]:
    """向现有指标字典附加并发数和每用户输出吞吐量。"""
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


def merge_request_measurements(
    dataset_measurements: list[dict[str, Any]],
) -> dict[str, Any]:
    """按数据集顺序合并请求结果，并累加各数据集的运行时间。"""
    if not dataset_measurements:
        raise ValueError(
            "merge_request_measurements requires at least one dataset"
        )
    results: list[Any] = []
    total_time = 0.0
    for measurements in dataset_measurements:
        results.extend(measurements["results"])
        total_time += float(measurements["total_time"])
    return {"results": results, "total_time": total_time}


def summarize_request_measurements(output: dict[str, Any]) -> dict[str, Any]:
    """把一个 HTTP 负载点的逐请求结果聚合为既有指标结构。"""
    results = output["results"]
    successful = [result for result in results if result["success"]]

    stream_modes = {bool(result["stream"]) for result in results}
    if not results:
        streamed = True
    elif len(stream_modes) != 1:
        raise ValueError(f"mixed stream modes in one run: {sorted(stream_modes)}")
    else:
        streamed = stream_modes.pop()

    latencies = [float(result["latency"]) for result in successful]
    if streamed:
        ttfts = [
            float(result["ttft"])
            for result in successful
            if result["ttft"] is not None
        ]
        tpots = [
            float(result["tpot"])
            for result in successful
            if result["tpot"] is not None
        ]
        itls = [
            float(value)
            for result in successful
            for value in result.get("itl_samples", [])
        ]
    else:
        ttfts = []
        tpots = []
        itls = []

    output_tokens = sum(int(result["output_tokens"]) for result in successful)
    input_tokens = sum(int(result["input_tokens"]) for result in successful)
    total_time = float(output["total_time"])
    success_count = len(successful)
    failed_count = len(results) - success_count

    return {
        "request_num": len(results),
        "success_num": success_count,
        "failed_num": failed_count,
        "success_rate": success_count / len(results) if results else 0.0,
        "stream": streamed,
        "latency": percentile_summary(latencies),
        "ttft": percentile_summary(ttfts),
        "tpot": percentile_summary(tpots),
        "itl": percentile_summary(itls),
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
