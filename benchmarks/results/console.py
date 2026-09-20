# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure console logging and print HTTP benchmark configuration and results."""

from __future__ import annotations

import logging
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService

logger = logging.getLogger(__name__)


def configure_logging(console_enabled: bool) -> None:
    """Configure console logging and keep HTTP library logs from interfering with progress output."""
    logging.basicConfig(
        level=logging.INFO if console_enabled else logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_model_service(service: ModelService) -> None:
    """Print the public model service selected from the Foretoken deployment."""
    print(f"Model service: {service.chat_completions_url}")
    if service.hostname:
        print(f"Hostname: {service.hostname}")
    print(f"Models: {', '.join(service.models)}")


def format_benchmark_config(
    benchmark: BenchmarkConfig,
    service: ModelService,
) -> str:
    """Build a user-visible HTTP benchmark configuration summary."""
    dataset = benchmark.resolved_workload
    trace = benchmark.trace
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
        schedule = benchmark.load
        concurrency_label = (
            "no concurrency limit"
            if schedule.max_concurrency == -1
            else str(schedule.max_concurrency)
        )
        concurrency_line = f"  Concurrency: {concurrency_label}\n"
        request_count_label = str(schedule.request_count)
        if schedule.arrival_rate > 0:
            arrival_rate_label = f"{schedule.arrival_rate:g} req/s (Poisson arrivals)"
        else:
            arrival_rate_label = "no rate limit"
        trace_lines = ""

    count_name = "Work items"
    max_turns_line = (
        f"  Max turns  : {'dataset-defined' if dataset.max_turns == -1 else dataset.max_turns}\n"
        if dataset.dataset_selectors and not trace.trace_selector
        else ""
    )
    return (
        "\n===== Foretoken Benchmark Configuration ====\n"
        f"  URL        : {service.chat_completions_url}\n"
        f"  Model      : {service.model}\n"
        f"{concurrency_line}"
        f"  {count_name:<11}: {request_count_label}\n"
        f"  Arrival rate: {arrival_rate_label}\n"
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


def _percentile_row(
    name: str,
    stats: dict[str, Any],
    unit: str = "s",
    *,
    scale: float = 1.0,
) -> str:
    """Build a two-decimal mean, p50, p95, and p99 row in the display unit."""
    values = {
        key: None if stats[key] is None else float(stats[key]) * scale
        for key in ("mean", "p50", "p95", "p99")
    }
    return (
        f"  {name:<12} mean={_format_metric(values['mean'], 2)}{unit}  "
        f"p50={_format_metric(values['p50'], 2)}{unit}  "
        f"p95={_format_metric(values['p95'], 2)}{unit}  "
        f"p99={_format_metric(values['p99'], 2)}{unit}"
    )


def log_benchmark_summary(run_record: dict[str, Any], metrics: dict[str, Any]) -> None:
    """Print a summary of workload settings, success rate, latency, and throughput."""
    resolved = run_record["resolved"]
    parallel = metrics["parallel"]
    throughput = metrics["throughput"]
    generation_tokens_per_second = throughput[
        "generation_tokens_per_second"
    ]
    multi_turn = isinstance(metrics.get("conversation"), dict)

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

    rate = resolved["rate"]

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {run_record['model']}",
    ]
    if multi_turn:
        conversation = metrics["conversation"]
        lines.extend(
            [
                f"  Requests   : {metrics['request_num']}",
                "  Multi-turn conversations attempted: "
                f"{conversation['attempted_num']}",
                f"  Multi-turn requests: {conversation['request_num']}",
                f"  {concurrency_label}: {concurrency_value}",
            ]
        )
    else:
        lines.extend(
            [
                f"  Requests   : {metrics['request_num']}",
                f"  {concurrency_label:<11}: {concurrency_value}",
            ]
        )
    if run_record.get("datasets"):
        lines.append(f"  Datasets   : {run_record['datasets']}")
    elif run_record.get("dataset"):
        lines.append(f"  Dataset    : {run_record['dataset']}")
    if float(rate) > 0:
        lines.append(f"  Arrival rate: {rate} req/s")
    stream = bool(metrics["stream"])
    metric_lines = [
        _percentile_row("End-to-end latency (E2EL)", metrics["latency"]),
    ]
    if stream:
        metric_lines.append(_percentile_row("TTFT", metrics["ttft"]))
    if run_record.get("trace_path"):
        metric_lines.append(
            _percentile_row("Replay delay", metrics["replay_delay"])
        )
        if stream:
            metric_lines.append(
                _percentile_row(
                    "TTFT including replay delay", metrics["trace_e2e_ttft"]
                )
            )
        metric_lines.append(
            _percentile_row(
                "E2EL including replay delay", metrics["trace_e2e_latency"]
            )
        )
    if stream:
        metric_lines.extend(
            [
                _percentile_row("TPOT", metrics["tpot"], "ms", scale=1000.0),
                _percentile_row("ITL", metrics["itl"], "ms", scale=1000.0),
            ]
        )

    success_label = "Successful requests" if multi_turn else "Success"
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
                        "Time to final-answer token (TTFAT)",
                        conversation["time_to_final_answer_token"],
                    ),
                ]
            )
        lines.append(
            "  Conversations/s attempted: "
            f"{_format_metric(conversation['attempted_conversations_per_second'])}"
        )
    lines.append(
        "  Mean tokens per successful request: "
        f"input={_format_metric(metrics['avg_input_tokens'], 2)}, "
        f"output={_format_metric(metrics['avg_output_tokens'], 2)}"
    )
    lines.extend(
        [
            f"  Request throughput (req/s): {_format_metric(throughput['requests_per_second'])}",
            f"  Input token throughput (tokens/s): "
            f"{_format_metric(throughput['prompt_tokens_per_second'])}",
            f"  Output token throughput (tokens/s): "
            f"{_format_metric(generation_tokens_per_second)}",
        ]
    )
    normalized = throughput.get(
        "generation_tokens_per_second_per_user"
    )
    if normalized is not None:
        lines.append(
            "  Output tok/s / user:"
            f"{_format_metric(normalized)}"
        )
    per_gpu = throughput.get("generation_tokens_per_second_per_gpu")
    if per_gpu is not None:
        lines.append(
            "  Output token throughput per GPU (tokens/s): "
            f"{_format_metric(per_gpu)}"
        )
    if metrics.get("avg_cached_input_tokens") is not None:
        lines.append(
            "  Mean reported cached input tokens: "
            f"{_format_metric(metrics['avg_cached_input_tokens'])}"
        )
    lines.extend(
        [
            f"  Benchmark duration (s): {_format_metric(metrics['benchmark_time'])}",
            "============================================",
        ]
    )
    logger.info("\n%s", "\n".join(lines))


def log_sweep_results(results: list[dict[str, Any]]) -> None:
    """Print one summary row for each parameter sweep result."""
    per_worker_name = "Output tokens/s/user"
    header = (
        f"{'Concurrency':>12} {'Arrival rate':>12} {'Work items':>10} "
        f"{'Output tokens/s':>15} {per_worker_name:>32} "
        f"{'Output tokens/s/GPU':>19} {'P99 E2EL (s)':>12}"
    )
    lines = [
        "========== Parameter Sweep Results =========",
        header,
    ]
    for item in results:
        parallel = item["parallel"]
        parallel_label = (
            "unlimited" if int(parallel) < 0 else str(int(parallel))
        )
        rate = float(item["rate"])
        rate_label = "no limit" if rate == -1 else f"{rate:g}"
        throughput = item["throughput"]
        generation_tokens_per_second = throughput[
            "generation_tokens_per_second"
        ]
        generation_tokens_per_second_per_concurrency = throughput.get(
            "generation_tokens_per_second_per_user"
        )
        generation_tokens_per_second_per_gpu_value = throughput.get(
            "generation_tokens_per_second_per_gpu"
        )
        lines.append(
            f"{parallel_label:>12} {rate_label:>12} {int(item['number']):>10} "
            f"{_format_metric(generation_tokens_per_second, 2):>15} "
            f"{_format_metric(generation_tokens_per_second_per_concurrency, 2):>32} "
            f"{_format_metric(generation_tokens_per_second_per_gpu_value, 2):>19} "
            f"{_format_metric(item['latency']['p99'], 3):>12}"
        )
    lines.append("============================================")
    logger.info("\n%s", "\n".join(lines))
