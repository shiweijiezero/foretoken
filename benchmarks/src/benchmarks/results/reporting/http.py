# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present HTTP benchmark results and materialize their local report artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from tabulate import tabulate

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.reporting.local import LocalDirectorySink
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.http import publish_http_wandb
from benchmarks.results.wandb.recording import WandbSink, wandb_run_timestamp


def http_result_sinks(
    benchmark: BenchmarkConfig,
    service: ModelService,
    directory: str,
    *,
    label: str | None,
    wandb_group: str | None,
) -> list[ResultSink]:
    """Compose HTTP report destinations; ResultOutputs owns their opening and cleanup."""
    outputs = benchmark.outputs
    sinks: list[ResultSink] = [ConsoleSink()]
    if outputs.saves_local:
        sinks.append(LocalDirectorySink(directory))
    if outputs.includes("wandb"):
        base_name = (
            benchmark.wandb.run_name.strip()
            or f"{service.model or 'dataset-models'}_{wandb_run_timestamp()}"
        )
        run_name = f"{base_name}_{label}" if label else base_name
        group = benchmark.wandb.group.strip() or wandb_group or ""
        run_config = benchmark.to_dict()
        run_config["service"]["model"] = service.model
        run_config["model"] = service.model
        if service.model_service_refs:
            run_config["declared_gpu_count"] = service.gpu_count
        sinks.append(WandbSink(
            benchmark,
            execution_dir=directory,
            run_name=run_name,
            group=group,
            publisher=publish_http_wandb,
            run_config=run_config,
        ))
    return sinks


class ConsoleSink:
    """Print the HTTP run summary to the console log."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        log_benchmark_summary(run.record, run.metrics)

    def close(self, *, exit_code: int = 0) -> None:
        return None


def log_model_service(service: ModelService) -> None:
    """Report the selected deployment service through the benchmark's diagnostic outputs."""
    logger.info("Model service: {}", service.chat_completions_url)
    if service.hostname:
        logger.info("Hostname: {}", service.hostname)
    logger.info("Models: {}", ", ".join(service.models))


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
            f"{benchmark.load.max_concurrency if benchmark.load.max_concurrency > 0 else 'no limit'}\n"
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
            unit = "conv/s" if benchmark.is_multi_turn else "req/s"
            arrival_rate_label = f"{schedule.arrival_rate:g} {unit} ({schedule.arrival_pattern} arrivals)"
        else:
            arrival_rate_label = "no rate limit"
        trace_lines = ""

    count_name = "Work items"
    max_turns_line = (
        f"  Max turns  : {'dataset-defined' if dataset.max_turns == -1 else dataset.max_turns}\n"
        if dataset.dataset_selectors and not trace.trace_selector
        else ""
    )
    slo = benchmark.slo
    if slo.params:
        params_label = str(slo.params)
        slo_lines = f"  SLO params : {params_label}\n"
        if slo.search:
            slo_lines += (
                f"  SLO concurrency bounds="
                f"[{slo.lower_bound}, {slo.upper_bound if slo.upper_bound is not None else 'none'}], "
                f"num_runs={slo.num_runs}\n"
            )
    else:
        slo_lines = ""
    return (
        "\n===== Foretoken Benchmark Configuration ====\n"
        f"  URL        : {service.chat_completions_url}\n"
        f"  Model      : {service.model or 'per dataset row'}\n"
        f"{concurrency_line}"
        f"  {count_name:<11}: {request_count_label}\n"
        f"  Arrival rate: {arrival_rate_label}\n"
        f"  Stream     : {benchmark.generation.stream}\n"
        f"  Dataset    : {dataset_label}\n"
        f"{max_turns_line}"
        f"{trace_lines}"
        f"{slo_lines}"
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
    parallel = metrics["max_concurrency"]
    throughput = metrics["throughput"]
    generation_tokens_per_second = throughput[
        "generation_tokens_per_second"
    ]
    multi_turn = isinstance(metrics.get("conversation"), dict)

    if run_record.get("trace_path"):
        concurrency_label = "Trace concurrency limit"
        concurrency_value = (
            "no concurrency limit" if int(parallel) < 0 else str(parallel)
        )
    elif int(parallel) < 0:
        concurrency_label = "Concurrency limit"
        concurrency_value = "no concurrency limit"
    else:
        concurrency_label = (
            "Conversation concurrency limit" if multi_turn else "Request concurrency limit"
        )
        concurrency_value = str(parallel)

    rate = resolved["request_rate"]

    lines = [
        "======== Foretoken Benchmark Result ========",
        f"  Model      : {run_record['model']}",
    ]
    if multi_turn:
        conversation = metrics["conversation"]
        lines.extend(
            [
                f"  Requests   : {metrics['request_num']}",
                f"  Multi-turn conversations attempted: {conversation['attempted_num']}",
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
    if "planned_num" in metrics:
        planned = metrics["planned_num"]
        lines.append(f"  Prepared requests: {planned if planned is not None else 'not fixed (duration-limited run)'}")
        lines.append(f"  Failed requests: {metrics['failed_num']}")
        lines.append(f"  Not executed: {metrics['not_executed_num']}")
        for reason, count in metrics["not_executed_reasons"].items():
            if count:
                lines.append(f"    {reason}: {count}")
    for reason in metrics.get("failure_reasons", []):
        lines.append(f"  Request failure ({reason['request_num']}): HTTP {reason['status_code']}, {reason['error']}")
    if run_record.get("datasets"):
        lines.append(f"  Datasets   : {run_record['datasets']}")
    elif run_record.get("dataset"):
        lines.append(f"  Dataset    : {run_record['dataset']}")
    if float(rate) > 0:
        lines.append(f"  Arrival rate: {rate} {'conv/s' if multi_turn else 'req/s'}")
    # Non-streaming requests have no observable token timing; trace delays retain their own clock.
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
            (
                f"  {success_label}: {metrics['success_num']}/{metrics['request_num']} "
                f"({float(metrics['success_rate']) * 100:.2f}%)"
            ),
            (
                "  Observed in-flight requests: "
                f"peak={metrics['request_concurrency']['peak']} "
                f"mean={_format_metric(metrics['request_concurrency']['mean'], 2)}"
            ),
            *metric_lines,
        ]
    )
    slo = metrics.get("slo")
    if isinstance(slo, dict) and slo.get("slo_attainment") is not None:
        lines.extend(
            [
                f"  SLO attainment (%): {_format_metric(float(slo['slo_attainment']) * 100)}",
                f"  SLO request goodput (req/s): {_format_metric(slo.get('request_goodput'))}",
                f"  SLO token goodput (tokens/s): {_format_metric(slo.get('token_goodput'))}",
            ]
        )
    # Serving-side estimates and allocation coverage remain distinct from HTTP completion statistics.
    speculative = metrics.get("speculative_decoding")
    if isinstance(speculative, dict) and any(
        speculative.get(key) is not None
        for key in ("acceptance_ratio", "draft_mean_seconds", "target_forward_mean_seconds")
    ):
        lines.append("  Speculative decoding (Prometheus window estimate):")
        for key, label, scale, unit in (
            ("acceptance_ratio", "Accepted draft tokens", 100, "%"),
            ("accepted_tokens_per_draft", "Accepted tokens per draft", 1, ""),
            ("draft_mean_seconds", "Draft GPU time/step", 1000, "ms"),
            ("target_forward_mean_seconds", "Target forward GPU time/step", 1000, "ms"),
            ("draft_time_share_ratio", "Draft share of measured GPU time", 100, "%"),
            ("target_forward_time_share_ratio", "Target forward share of measured GPU time", 100, "%"),
        ):
            value = speculative.get(key)
            measured = value * scale if value is not None else None
            lines.append(f"    {label}: {_format_metric(measured, 2)}{unit}")
    allocation = metrics.get("gpu_allocation")
    if isinstance(allocation, dict):
        coverage = allocation["coverage"]
        lines.append(
            "  GPU allocation observation coverage: "
            f"{_format_metric(coverage * 100 if coverage is not None else None, 2)}%"
        )
        for resource in sorted(set(allocation["observed_gpu_seconds"]) | set(allocation["gpu_seconds"] or {})):
            seconds = (allocation["gpu_seconds"] or {}).get(resource)
            hours = (allocation["gpu_hours"] or {}).get(resource)
            lines.append(
                f"    {resource}: GPU-seconds={_format_metric(seconds, 2)}, "
                f"GPU-hours={_format_metric(hours, 4)}"
            )
            if seconds is None and resource in allocation["observed_gpu_seconds"]:
                lines.append(
                    "      Observed GPU-seconds (partial, not total): "
                    f"{_format_metric(allocation['observed_gpu_seconds'][resource], 2)}"
                )
    if multi_turn:
        conversation = metrics["conversation"]
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
            f"  Input token throughput (tokens/s): {_format_metric(throughput['prompt_tokens_per_second'])}",
            f"  Output token throughput (tokens/s): {_format_metric(generation_tokens_per_second)}",
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
    for dimension, title in (("datasets", "Dataset"), ("models", "Model"), ("request_classes", "Request class")):
        groups = metrics.get(dimension)
        if not groups:
            continue
        lines.append(f"  {title} breakdown (shared {metrics['benchmark_time']:.2f}s window):")
        rows = []
        for name, group in groups.items():
            group_slo = group.get("slo") or {}
            attainment = group_slo.get("slo_attainment")
            display_name = Path(name).name if Path(name).is_absolute() else name
            rows.append([
                display_name, f"{group['success_num']}/{group['request_num']}",
                _format_metric(group['throughput']['requests_per_second'], 2),
                _format_metric(group['throughput']['generation_tokens_per_second'], 2),
                _format_metric(group['latency']['p95'], 3),
                _format_metric(attainment * 100 if attainment is not None else None, 1),
                _format_metric(group_slo.get('request_goodput'), 2),
            ])
        lines.append(tabulate(
            rows, headers=("Name", "Success", "Req/s", "Out tok/s", "P95 E2EL", "SLO %", "Good req/s"),
            tablefmt="plain", disable_numparse=True,
            colalign=("left", "right", "right", "right", "right", "right", "right"),
        ))
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
    logger.info("\n{}", "\n".join(lines))


def log_slo_results(slo: dict[str, Any]) -> None:
    """Show measured request peaks separately from configured limits and explain termination."""
    reasons = {
        "observed_concurrency_not_increasing": "observed request concurrency did not increase",
        "upper_bound_reached": "configured upper bound reached",
        "slo_boundary_found": "SLO boundary reached",
    }
    unit = slo["concurrency_limit_unit"]
    lines = ["========== SLO Concurrency Search Results =========="]
    for row in slo["probes"]:
        lines.append(
            f"  Group {row['group']}: {unit} limit={row['max_concurrency']} "
            f"request peak={row['peak_request_concurrency']} "
            f"repeat peaks={row['repeat_peak_request_concurrency']} "
            f"satisfied={row['satisfied']}"
        )
    for group in slo["groups"]:
        lines.extend([
            (
                f"  Group {group['group']}: best passing request peak="
                f"{_format_metric(group['best_peak_request_concurrency'], 0)} "
                f"at {unit} limit={_format_metric(group['best_max_concurrency'], 0)}"
            ),
            (
                f"  Stopped: {reasons[group['stop_reason']]}; "
                f"last request peak={group['last_peak_request_concurrency']} "
                f"at {unit} limit={group['last_max_concurrency']}"
            ),
        ])
    lines.append("===================================================")
    logger.info("\n{}", "\n".join(lines))


def log_sweep_results(results: list[dict[str, Any]]) -> None:
    """Print one summary row for each parameter sweep result."""
    headers = (
        "Method / group / run", "Status", "Concurrency", "Arrival rate", "Requests",
        "Output tokens/s", "Output tokens/s/user", "Output tokens/s/GPU", "P99 E2EL (s)",
    )
    rows = []
    for item in results:
        method = item["bench"].get("service", {}).get("name", "default")
        identity = f"{method} / {item['parameter_group']} / {item['run_number'] + 1}"
        if "slo_search" in item and "throughput" not in item:
            rows.append([identity, item["status"], "—", "—", "—", "—", "—", "—", "No passing SLO probe"])
            continue
        parallel = item["max_concurrency"]
        parallel_label = (
            "unlimited" if int(parallel) < 0 else str(int(parallel))
        )
        rate = float(item["request_rate"])
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
        rows.append([
            identity, item["status"], parallel_label, rate_label, str(item["request_num"]),
            _format_metric(generation_tokens_per_second, 2),
            _format_metric(generation_tokens_per_second_per_concurrency, 2),
            _format_metric(generation_tokens_per_second_per_gpu_value, 2),
            _format_metric(item["latency"]["p99"], 3),
        ])
    logger.info("\n{}\n{}\n{}", "========== Parameter Sweep Results =========", tabulate(
        rows, headers=headers, tablefmt="plain", disable_numparse=True,
        colalign=("left", "left", "right", "right", "right", "right", "right", "right", "right"),
    ), "============================================")
