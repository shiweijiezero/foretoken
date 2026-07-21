from __future__ import annotations

from typing import Any, Optional

import numpy as np


def _percentile_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }
    arr = np.asarray(values, dtype=float)
    return {
        "mean": float(np.mean(arr)),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
    }


def compute_tpot(
    latency: float,
    ttft: Optional[float],
    output_tokens: int,
) -> Optional[float]:
    if ttft is None:
        return None
    denom = max(int(output_tokens) - 1, 1)
    return max(latency - ttft, 0.0) / denom


def user_count_for_throughput(parallel: Optional[int]) -> int:
    """Denominator for per-user throughput.

    Closed-loop: ``parallel`` concurrent users.
    Open-loop (``parallel=-1``) or missing: treat as 1 (same as Pareto axis).
    """
    if parallel is None:
        return 1
    return max(int(parallel), 1)


def tokens_per_s_per_user(
    token_s: float,
    parallel: Optional[int],
) -> float:
    """``token/s / max(parallel, 1)`` (open-loop parallel=-1 → ÷1)."""
    return float(token_s) / float(user_count_for_throughput(parallel))


def attach_user_throughput(
    metrics: dict[str, Any],
    *,
    parallel: Optional[int] = None,
) -> dict[str, Any]:
    """Write ``throughput['token/s/user']`` using ``parallel`` (or metrics)."""
    if parallel is not None:
        metrics["parallel"] = int(parallel)
    conc = metrics.get("parallel")
    if conc is None and metrics.get("concurrency") is not None:
        conc = int(metrics["concurrency"])

    throughput = metrics.setdefault("throughput", {})
    token_s = float(
        throughput.get("token/s")
        or throughput.get("output_token/s")
        or 0.0
    )
    throughput["token/s/user"] = tokens_per_s_per_user(token_s, conc)
    return metrics


class MetricsAggregator:

    def aggregate(self, output: dict[str, Any]) -> dict[str, Any]:
        results = output["results"]
        success_results = [r for r in results if r.get("success")]

        latencies = [float(r["latency"]) for r in success_results]

        ttfts = [
            float(r["ttft"])
            for r in success_results
            if r.get("ttft") is not None
        ]

        tpots: list[float] = []
        for r in success_results:
            tpot = r.get("tpot")
            if tpot is None:
                tpot = compute_tpot(
                    float(r["latency"]),
                    r.get("ttft"),
                    int(r.get("output_tokens", 0) or 0),
                )
            if tpot is not None:
                tpots.append(float(tpot))

        output_tokens = sum(
            int(r.get("output_tokens", 0) or 0) for r in success_results
        )
        input_tokens = sum(
            int(r.get("input_tokens", 0) or 0) for r in success_results
        )
        total_time = float(output["total_time"])
        success_num = len(success_results)
        failed_num = len(results) - success_num

        # Multi-turn: average user turns attached by the runner.
        turns = [
            float(r["num_input_turns"])
            for r in success_results
            if r.get("num_input_turns") is not None
            and float(r["num_input_turns"]) > 0
        ]
        avg_turns = (
            float(np.mean(turns)) if turns else 0.0
        )

        return {
            "request_num": len(results),
            "success_num": success_num,
            "failed_num": failed_num,
            "success_rate": (
                success_num / len(results) if results else 0.0
            ),
            "latency": _percentile_stats(latencies),
            "ttft": _percentile_stats(ttfts),
            "tpot": _percentile_stats(tpots),
            # ITL ≈ TPOT when per-token gaps are not recorded.
            "itl": _percentile_stats(tpots),
            "throughput": {
                "request/s": (
                    len(results) / total_time if total_time > 0 else 0.0
                ),
                # EvalScope: output tok/s vs wall time
                "token/s": (
                    output_tokens / total_time if total_time > 0 else 0.0
                ),
                "output_token/s": (
                    output_tokens / total_time if total_time > 0 else 0.0
                ),
                "input_token/s": (
                    input_tokens / total_time if total_time > 0 else 0.0
                ),
                "total_token/s": (
                    (input_tokens + output_tokens) / total_time
                    if total_time > 0
                    else 0.0
                ),
            },
            "avg_input_tokens": (
                input_tokens / success_num if success_num else 0.0
            ),
            "avg_output_tokens": (
                output_tokens / success_num if success_num else 0.0
            ),
            "avg_turns_per_request": avg_turns,
            "benchmark_time": total_time,
        }
