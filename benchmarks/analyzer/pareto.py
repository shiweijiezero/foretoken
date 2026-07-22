"""Pareto frontier for sweep results.

Current axes (vLLM-aligned):
  X = tokens/s/user = token_throughput / parallel
  Y = tokens/s/GPU  = token_throughput / gpu_count

Additional axis modes can be added later.
"""

from __future__ import annotations

from typing import Any

from benchmarks.arguments import metric_parallel
from benchmarks.analyzer.vllm_pareto import (
    prepare_vllm_pareto_rows,
    token_throughput,
    vllm_pareto_available,
    vllm_pareto_frontier_indices,
)


def attach_pareto_metrics(
    results: list[dict[str, Any]],
    *,
    gpu_count: int = 1,
) -> list[dict[str, Any]]:
    """Attach tokens/s/user and tokens/s/GPU axes on each point."""
    gpus = max(int(gpu_count), 1)

    prepared_by_index: dict[int, dict[str, Any]] = {}
    if vllm_pareto_available():
        for row in prepare_vllm_pareto_rows(results, gpu_count=gpus):
            prepared_by_index[int(row["_foretoken_index"])] = row

    for index, item in enumerate(results):
        tok_s = token_throughput(item)
        # Open-loop parallel=-1: treat as 1 for per-user axis only.
        parallel = max(metric_parallel(item), 1)
        prepared = prepared_by_index.get(index)
        if prepared is not None:
            item["pareto"] = {
                "gpu_count": float(prepared.get("gpu_count", gpus)),
                "token_s": tok_s,
                "token_s_per_user": float(prepared["tokens_per_user"]),
                "token_s_per_gpu": float(prepared["tokens_per_gpu"]),
                "backend": "vllm",
            }
        else:
            item_gpus = max(int(item.get("gpu_count") or gpus), 1)
            item["pareto"] = {
                "gpu_count": item_gpus,
                "token_s": tok_s,
                "token_s_per_user": tok_s / parallel,
                "token_s_per_gpu": tok_s / item_gpus,
                "backend": "foretoken",
            }
        # Keep client throughput mirror in sync with Pareto X axis.
        throughput = item.setdefault("throughput", {})
        throughput["token/s/user"] = float(
            item["pareto"]["token_s_per_user"]
        )
    return results


def pareto_xy(item: dict[str, Any]) -> tuple[float, float]:
    axes = item.get("pareto") or {}
    return (
        float(axes.get("token_s_per_user", 0.0)),
        float(axes.get("token_s_per_gpu", 0.0)),
    )


def _dominates(other: dict[str, Any], item: dict[str, Any]) -> bool:
    """Maximize tokens/s/user (x) and tokens/s/GPU (y)."""
    ox, oy = pareto_xy(other)
    ix, iy = pareto_xy(item)
    better_or_eq = ox >= ix and oy >= iy
    strictly_better = ox > ix or oy > iy
    return better_or_eq and strictly_better


def _frontier_fallback(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for item in results:
        dominated = any(
            _dominates(other, item) for other in results if other is not item
        )
        if not dominated:
            frontier.append(item)
    frontier.sort(
        key=lambda r: (metric_parallel(r, default=0), pareto_xy(r)[0])
    )
    return frontier


def pareto_frontier(
    results: list[dict[str, Any]],
    *,
    gpu_count: int = 1,
) -> list[dict[str, Any]]:
    """Return non-dominated points (tokens/s/user vs tokens/s/GPU)."""
    if not results:
        return []

    if any("pareto" not in r for r in results):
        attach_pareto_metrics(results, gpu_count=gpu_count)

    if vllm_pareto_available():
        prepared = prepare_vllm_pareto_rows(results, gpu_count=gpu_count)
        indices = vllm_pareto_frontier_indices(prepared)
        frontier = [results[i] for i in indices]
        frontier.sort(
            key=lambda r: (metric_parallel(r, default=0), pareto_xy(r)[0])
        )
        return frontier

    return _frontier_fallback(results)
