# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure console logging and print HTTP benchmark configuration and results."""

from __future__ import annotations

import logging
from typing import Any

from benchmarks.performance.config import HttpBenchmarkConfig
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint
from benchmarks.performance.request_metrics import generation_tokens_per_second_per_gpu

logger = logging.getLogger(__name__)


def configure_logging(console_enabled: bool) -> None:
    """Configure console logging and keep HTTP library logs from interfering with progress output."""
    logging.basicConfig(
        level=logging.INFO if console_enabled else logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_benchmark_endpoint(
    endpoint_url: str,
    models: tuple[str, ...],
    hostname: str,
) -> None:
    """Print the public benchmark endpoint selected from the Foretoken deployment."""
    print(f"Endpoint: {endpoint_url}")
    if hostname:
        print(f"Hostname: {hostname}")
    print(f"Models: {', '.join(models)}")


def format_benchmark_config(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
) -> str:
    """Build a user-visible HTTP benchmark configuration summary."""
    dataset = benchmark.request_dataset
    trace = benchmark.arrival_trace
    if trace.trace_selector:
        dataset_label = (
            f"trace={trace.trace_selector}, "
            f"dataset={dataset.dataset_selectors[0]}"
        )
    elif dataset.fixed_prompt:
        dataset_label = "prompt=<fixed>"
    elif dataset.dataset_selectors == ["random"]:
        dataset_label = (
            "random "
            f"(prefix={dataset.shared_prefix_tokens}, "
            f"min={dataset.minimum_prompt_tokens}, "
            f"max={dataset.maximum_prompt_tokens})"
        )
    elif dataset.has_multiple_datasets:
        dataset_label = (
            f"{dataset.dataset_selectors} (total number across all)"
        )
    else:
        dataset_label = (
            dataset.dataset_selectors[0]
            if dataset.dataset_selectors
            else "<none>"
        )

    if trace.trace_selector:
        concurrency_line = ""
        request_count_label = "trace-driven"
        arrival_rate_label = "trace timestamps"
        open_loop_line = ""
        duration = (
            "until end"
            if trace.duration_seconds is None
            else f"{trace.duration_seconds:g}s"
        )
        trace_lines = (
            f"  Trace Window: start={trace.start_offset_seconds:g}s, "
            f"duration={duration}\n"
            "  Trace concurrency: "
            f"{trace.max_concurrency or 'no limit'}\n"
        )
        if trace.synthetic_prefix_reuse:
            trace_lines += "  Trace Prefix: synthetic hash-id blocks\n"
    else:
        schedule = benchmark.load_schedule
        concurrency_label = (
            "no concurrency limit"
            if schedule.unbounded_concurrency
            else str(schedule.max_concurrency)
        )
        concurrency_name = (
            "Concurrent conversations"
            if benchmark.is_multi_turn
            else "Concurrency"
        )
        concurrency_line = f"  {concurrency_name}: {concurrency_label}\n"
        request_count_label = str(schedule.request_count)
        if schedule.arrival_rate > 0:
            mode = (
                "open-loop"
                if schedule.unbounded_concurrency
                else "closed-loop"
            )
            arrival_rate_label = (
                f"{schedule.arrival_rate:g} req/s ({mode}, Poisson arrivals)"
            )
        else:
            arrival_rate_label = "no rate limit"
        open_loop_line = (
            f"  Open-loop  : {schedule.unbounded_concurrency}\n"
        )
        trace_lines = ""

    count_name = "Conversations" if benchmark.is_multi_turn else "Requests"
    max_turns_line = (
        f"  Max turns  : {'dataset-defined' if dataset.max_turns == -1 else dataset.max_turns}\n"
        if benchmark.is_multi_turn
        else ""
    )
    return (
        "\n===== Foretoken Benchmark Configuration ====\n"
        f"  URL        : {endpoint.url}\n"
        f"  Model      : {endpoint.model}\n"
        f"{concurrency_line}"
        f"  {count_name:<11}: {request_count_label}\n"
        f"  Arrival rate: {arrival_rate_label}\n"
        f"{open_loop_line}"
        f"  Stream     : {benchmark.generation.stream}\n"
        f"  Dataset    : {dataset_label}\n"
        f"{max_turns_line}"
        f"{trace_lines}"
        "============================================\n"
    )


def _format_metric(value: Any, digits: int = 4) -> str:
    """Format a metric value with fixed precision, displaying missing values as a dash."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _percentile_row(name: str, stats: dict[str, Any], unit: str = "s") -> str:
    """Build an output row with the mean, p50, p95, and p99 for one metric."""
    return (
        f"  {name:<12} mean={_format_metric(stats['mean'])}{unit}  "
        f"p50={_format_metric(stats['p50'])}{unit}  "
        f"p95={_format_metric(stats['p95'])}{unit}  "
        f"p99={_format_metric(stats['p99'])}{unit}"
    )


def log_benchmark_summary(run_record: dict[str, Any], metrics: dict[str, Any]) -> None:
    """Print a summary of workload settings, success rate, latency, and throughput."""
    resolved = run_record["resolved"]
    parallel = metrics["parallel"]
    throughput = metrics["throughput"]
    generation_tokens_per_second = throughput[
        "generation_tokens_per_second"
    ]
    multi_turn = bool(run_record.get("multi_turn"))

    if run_record.get("trace_path"):
        trace_max = run_record.get("trace_max_concurrency")
        concurrency_label = "Trace concurrency"
        concurrency_value = (
            "no concurrency limit" if trace_max is None else str(trace_max)
        )
    elif int(parallel) < 0:
        concurrency_label = "Concurrency"
        concurrency_value = "no concurrency limit"
    else:
        concurrency_label = (
            "Concurrent conversations" if multi_turn else "Concurrency"
        )
        concurrency_value = str(parallel)

    number = resolved["number"]
    rate = resolved["rate"]

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {run_record['model']}",
    ]
    if multi_turn:
        conversation = metrics["conversation"]
        lines.extend(
            [
                f"  Conversations attempted: {conversation['attempted_num']}",
                f"  Turn requests: {metrics['request_num']}",
                f"  {concurrency_label}: {concurrency_value}",
            ]
        )
    else:
        lines.extend(
            [
                f"  Requests   : {number}",
                f"  {concurrency_label:<11}: {concurrency_value}",
            ]
        )
    if run_record.get("datasets"):
        lines.append(f"  Datasets   : {run_record['datasets']}")
    elif run_record.get("dataset"):
        lines.append(f"  Dataset    : {run_record['dataset']}")
    if run_record["open_loop"]:
        lines.append("  Open-loop  : True")
    if float(rate) > 0:
        lines.append(f"  Arrival rate: {rate} req/s")
    metric_lines = [
        _percentile_row("Latency", metrics["latency"]),
        _percentile_row("TTFT", metrics["ttft"]),
    ]
    if run_record.get("trace_path"):
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

    success_label = "Successful turns" if multi_turn else "Success"
    lines.extend(
        [
            f"  {success_label}: {metrics['success_num']}/"
            f"{metrics['request_num']} "
            f"({float(metrics['success_rate']) * 100:.2f}%)",
            *metric_lines,
        ]
    )
    if multi_turn:
        conversation = metrics["conversation"]
        if conversation.get("per_dataset"):
            lines.append(
                "  Conversation distributions: see per-dataset child results"
            )
        else:
            lines.extend(
                [
                    _percentile_row(
                        "Conversation latency", conversation["latency"]
                    ),
                    _percentile_row(
                        "Final-answer TTFT",
                        conversation["time_to_final_answer_token"],
                    ),
                ]
            )
        lines.append(
            "  Conversations/s attempted: "
            f"{_format_metric(throughput['attempted_conversations_per_second'])}"
        )
    lines.extend(
        [
            f"  Turn requests/s: {_format_metric(throughput['requests_per_second'])}"
            if multi_turn
            else f"  Requests/s : {_format_metric(throughput['requests_per_second'])}",
            f"  Generation tokens/s: "
            f"{_format_metric(generation_tokens_per_second)}",
            f"  Benchmark time: {_format_metric(metrics['benchmark_time'])}s",
            "============================================",
        ]
    )
    if not run_record.get("trace_path"):
        denominator = "concurrent conversation" if multi_turn else "user"
        lines.insert(
            -2,
            f"  Generation tokens/s/{denominator}: "
            f"{_format_metric(throughput['generation_tokens_per_second_per_user'])}",
        )
    logger.info("\n%s", "\n".join(lines))


def log_sweep_results(results: list[dict[str, Any]]) -> None:
    """Print one summary row for each parameter sweep result."""
    multi_turn = bool(results and results[0].get("multi_turn"))
    concurrency_name = "Concurrent conv." if multi_turn else "Concurrency"
    count_name = "Conversations" if multi_turn else "Requests"
    per_worker_name = (
        "Generation tokens/s/conversation"
        if multi_turn
        else "Generation tokens/s/user"
    )
    header = (
        f"{concurrency_name:>12} {'Arrival rate':>12} {count_name:>8} "
        f"{'Generation tokens/s':>15} {per_worker_name:>32} "
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
            f"{_format_metric(generation_tokens_per_second_per_user, 2):>32} "
            f"{_format_metric(generation_tokens_per_second_per_gpu_value, 2):>19} "
            f"{_format_metric(item['latency']['p99'], 3):>12}"
        )
    lines.append("============================================")
    logger.info("\n%s", "\n".join(lines))
