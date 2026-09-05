# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Log a metrics summary."""

from __future__ import annotations

import logging
from typing import Any

from benchmarks.metrics.aggregator import generation_tokens_per_second_per_gpu

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
    generation_tokens_per_second = throughput[
        "generation_tokens_per_second"
    ]
    generation_tokens_per_second_per_user = throughput[
        "generation_tokens_per_second_per_user"
    ]

    if config.get("trace_path"):
        trace_max = config.get("trace_max_concurrency")
        concurrency_label = "Trace concurrency"
        concurrency_value = (
            "no concurrency limit" if trace_max is None else str(trace_max)
        )
    elif int(parallel) < 0:
        concurrency_label = "Concurrency"
        concurrency_value = "no concurrency limit"
    else:
        concurrency_label = "Concurrency"
        concurrency_value = str(parallel)

    number = resolved["number"]
    rate = resolved["rate"]

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {config['model']}",
        f"  Requests   : {number}",
        f"  {concurrency_label:<11}: {concurrency_value}",
    ]
    if config.get("datasets"):
        lines.append(f"  Datasets   : {config['datasets']}")
    elif config.get("dataset"):
        lines.append(f"  Dataset    : {config['dataset']}")
    if config["open_loop"]:
        lines.append("  Open-loop  : True")
    if float(rate) > 0:
        lines.append(f"  Arrival rate: {rate} req/s")
    metric_lines = [
        _percentile_row("Latency", metrics["latency"]),
        _percentile_row("TTFT", metrics["ttft"]),
    ]
    if config.get("trace_path"):
        metric_lines = [
            _percentile_row("Request latency", metrics["latency"]),
            _percentile_row("Request TTFT", metrics["ttft"]),
            _percentile_row("Replay delay", metrics["replay_delay"]),
            _percentile_row("End-to-end TTFT", metrics["trace_e2e_ttft"]),
            _percentile_row(
                "End-to-end latency", metrics["trace_e2e_latency"]
            ),
        ]
    metric_lines.append(_percentile_row("TPOT", metrics["tpot"]))

    lines.extend(
        [
            f"  Success    : {metrics['success_num']}/"
            f"{metrics['request_num']} "
            f"({float(metrics['success_rate']) * 100:.2f}%)",
            *metric_lines,
            f"  Requests/s : {_format_metric(throughput['requests_per_second'])}",
            f"  Generation tokens/s: "
            f"{_format_metric(generation_tokens_per_second)}",
            f"  Benchmark time: {_format_metric(metrics['benchmark_time'])}s",
            "============================================",
        ]
    )
    if not config.get("trace_path"):
        lines.insert(
            -2,
            f"  Generation tokens/s/user: "
            f"{_format_metric(generation_tokens_per_second_per_user)}",
        )
    logger.info("\n%s", "\n".join(lines))


def log_sweep_results(results: list[dict[str, Any]]) -> None:
    """Log one row per parameter-sweep point."""
    header = (
        f"{'Concurrency':>12} {'Arrival rate':>12} {'Requests':>8} "
        f"{'Generation tokens/s':>15} {'Generation tokens/s/user':>20} "
        f"{'Generation tokens/s/GPU':>19} {'P99 latency':>12}"
    )
    lines = [
        "========== Parameter Sweep Results =========",
        header,
    ]
    for item in results:
        parallel = item["parallel"]
        parallel_label = (
            "open-loop" if int(parallel) < 0 else str(int(parallel))
        )
        rate = float(item["rate"])
        rate_label = "no limit" if rate == -1 else f"{rate:g}"
        throughput = item["throughput"]
        generation_tokens_per_second = throughput[
            "generation_tokens_per_second"
        ]
        generation_tokens_per_second_per_user = throughput[
            "generation_tokens_per_second_per_user"
        ]
        generation_tokens_per_second_per_gpu_value = (
            generation_tokens_per_second_per_gpu(
                float(generation_tokens_per_second),
                int(item["gpu_count"]),
            )
        )
        lines.append(
            f"{parallel_label:>12} {rate_label:>12} {int(item['number']):>8} "
            f"{_format_metric(generation_tokens_per_second, 2):>15} "
            f"{_format_metric(generation_tokens_per_second_per_user, 2):>20} "
            f"{_format_metric(generation_tokens_per_second_per_gpu_value, 2):>19} "
            f"{_format_metric(item['latency']['p99'], 3):>12}"
        )
    lines.append("============================================")
    logger.info("\n%s", "\n".join(lines))
