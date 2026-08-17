# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Log a metrics summary."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _format_metric(value: Any, digits: int = 4) -> str:
    """Format a metric value for display; None becomes an em dash, otherwise a fixed-point float."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _percentile_row(name: str, stats: dict[str, Any], unit: str = "s") -> str:
    """Build one percentile line: mean / p50 / p95 / p99 for a named metric."""
    return (
        f"  {name:<12} mean={_format_metric(stats['mean'])}{unit}  "
        f"p50={_format_metric(stats['p50'])}{unit}  "
        f"p95={_format_metric(stats['p95'])}{unit}  "
        f"p99={_format_metric(stats['p99'])}{unit}"
    )


def log_summary(config: dict[str, Any], metrics: dict[str, Any]) -> None:
    """Log the benchmark banner: config, success rate, latency, throughput."""
    resolved = config["resolved"]
    parallel = metrics["parallel"]
    throughput = metrics["throughput"]

    if int(parallel) < 0:
        parallel_label = "unlimited (open-loop)"
    else:
        parallel_label = str(parallel)

    number = resolved["number"]
    rate = resolved["rate"]

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {config['model']}",
        f"  Requests   : {number}",
        f"  Parallel   : {parallel_label}",
    ]
    if config.get("datasets"):
        lines.append(f"  Datasets   : {config['datasets']}")
    elif config.get("dataset"):
        lines.append(f"  Dataset    : {config['dataset']}")
    if config["open_loop"]:
        lines.append("  Open Loop  : True")
    if float(rate) > 0:
        lines.append(f"  Rate       : {rate} req/s")
    lines.extend(
        [
            f"  Success    : {metrics['success_num']}/"
            f"{metrics['request_num']} "
            f"({float(metrics['success_rate']) * 100:.2f}%)",
            _percentile_row("Latency", metrics["latency"]),
            _percentile_row("TTFT", metrics["ttft"]),
            _percentile_row("TPOT", metrics["tpot"]),
            f"  Request/s  : {_format_metric(throughput['request/s'])}",
            f"  Token/s    : {_format_metric(throughput['token/s'])}",
            f"  Tok/s/user : {_format_metric(throughput['token/s/user'])}",
            f"  Wall time  : {_format_metric(metrics['benchmark_time'])}s",
            "============================================",
        ]
    )
    logger.info("\n%s", "\n".join(lines))
