# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Print a single-run metrics summary to stdout."""

from __future__ import annotations

from typing import Any


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct_row(name: str, stats: dict[str, Any] | None, unit: str = "s") -> str:
    stats = stats or {}
    return (
        f"  {name:<12} mean={_fmt(stats.get('mean'))}{unit}  "
        f"p50={_fmt(stats.get('p50'))}{unit}  "
        f"p95={_fmt(stats.get('p95'))}{unit}  "
        f"p99={_fmt(stats.get('p99'))}{unit}"
    )


def print_summary(config: dict[str, Any], metrics: dict[str, Any]) -> None:
    resolved = config.get("resolved") or {}
    parallel = metrics.get("parallel", resolved.get("parallel"))

    if parallel is not None and int(parallel) < 0:
        parallel_s = "unlimited (open-loop)"
    else:
        parallel_s = str(parallel)

    number = resolved.get("number", config.get("number", ""))
    rate = resolved.get("rate", config.get("rate"))
    throughput = metrics.get("throughput") or {}

    print()
    print("======== Foretoken Benchmark Result ========")
    print(f"  Model      : {config.get('model', '')}")
    print(f"  Requests   : {number}")
    print(f"  Parallel   : {parallel_s}")
    if config.get("open_loop"):
        print("  Open Loop  : True")
    if rate is not None and float(rate) > 0:
        print(f"  Rate       : {rate} req/s")
    print(
        f"  Success    : {metrics.get('success_num', 0)}/"
        f"{metrics.get('request_num', 0)} "
        f"({float(metrics.get('success_rate') or 0.0) * 100:.2f}%)"
    )
    print(_pct_row("Latency", metrics.get("latency")))
    print(_pct_row("TTFT", metrics.get("ttft")))
    print(_pct_row("TPOT", metrics.get("tpot")))
    print(f"  Request/s  : {_fmt(throughput.get('request/s'))}")
    print(f"  Token/s    : {_fmt(throughput.get('token/s'))}")
    print(f"  Tok/s/user : {_fmt(throughput.get('token/s/user'))}")
    print(f"  Wall time  : {_fmt(metrics.get('benchmark_time'))}s")
    print("============================================")
