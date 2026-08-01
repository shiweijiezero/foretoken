# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Log a metrics summary."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _fmt(value: Any, digits: int = 4) -> str:
    """Format a metric value for display; None becomes an em dash, otherwise a fixed-point float."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct_row(name: str, stats: dict[str, Any] | None, unit: str = "s") -> str:
    """Build one percentile line: mean / p50 / p95 / p99 for a named metric."""
    stats = stats or {}
    return (
        f"  {name:<12} mean={_fmt(stats.get('mean'))}{unit}  "
        f"p50={_fmt(stats.get('p50'))}{unit}  "
        f"p95={_fmt(stats.get('p95'))}{unit}  "
        f"p99={_fmt(stats.get('p99'))}{unit}"
    )


def log_summary(config: dict[str, Any], metrics: dict[str, Any]) -> None:
    """Log the benchmark banner: config, success rate, latency, throughput."""
    resolved = config.get("resolved") or {}
    parallel = metrics.get("parallel", resolved.get("parallel"))

    if parallel is not None and int(parallel) < 0:
        parallel_s = "unlimited (open-loop)"
    else:
        parallel_s = str(parallel)

    number = resolved.get("number", config.get("number", ""))
    rate = resolved.get("rate", config.get("rate"))
    throughput = metrics.get("throughput") or {}

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {config.get('model', '')}",
        f"  Requests   : {number}",
        f"  Parallel   : {parallel_s}",
    ]
    if config.get("open_loop"):
        lines.append("  Open Loop  : True")
    if rate is not None and float(rate) > 0:
        lines.append(f"  Rate       : {rate} req/s")
    lines.extend(
        [
            f"  Success    : {metrics.get('success_num', 0)}/"
            f"{metrics.get('request_num', 0)} "
            f"({float(metrics.get('success_rate') or 0.0) * 100:.2f}%)",
            _pct_row("Latency", metrics.get("latency")),
            _pct_row("TTFT", metrics.get("ttft")),
            _pct_row("TPOT", metrics.get("tpot")),
            f"  Request/s  : {_fmt(throughput.get('request/s'))}",
            f"  Token/s    : {_fmt(throughput.get('token/s'))}",
            f"  Tok/s/user : {_fmt(throughput.get('token/s/user'))}",
            f"  Wall time  : {_fmt(metrics.get('benchmark_time'))}s",
            "============================================",
        ]
    )
    logger.info("\n%s", "\n".join(lines))
