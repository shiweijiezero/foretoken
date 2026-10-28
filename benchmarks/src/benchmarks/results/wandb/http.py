# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Write HTTP benchmark results to an independent Weights & Biases run."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import wandb

from benchmarks.results.measurements.requests import percentile_summary

if TYPE_CHECKING:
    from benchmarks.results.types import BenchmarkRun
from benchmarks.results.measurements.timeseries import (
    ELAPSED_TIME,
    REQUEST_INDEX,
    cumulative_series,
    request_series,
    time_series,
)
from benchmarks.results.observations.replicas import (
    gpu_allocation_history_rows,
    replica_history_rows,
)
from benchmarks.results.recording.database import read_record
from benchmarks.results.recording.requests import read_request_records
from benchmarks.results.wandb.axes import TRACE_TIME, define_axis_metrics

_TIME_TAKEN = "Benchmark duration (s)"
_CONCURRENCY = "Concurrency limit"
_REQUEST_RATE = "Arrival rate (req/s)"
_TOTAL_REQUESTS = "Requests"
_SUCCEED_REQUESTS = "Successful requests"
_FAILED_REQUESTS = "Failed requests"
_REQUESTS_PER_SECOND = "Request throughput (req/s)"
_SUCCESS_RATE = "Success rate (%)"
_AVERAGE_INPUT_TOKENS = "Mean input tokens"
_INPUT_TOKENS_PER_SECOND = "Input token throughput (tokens/s)"
_GENERATION_TOKENS_PER_SECOND = "Output token throughput (tokens/s)"
_TOTAL_TOKENS_PER_SECOND = "Total tokens per second (tokens/s)"
_AVERAGE_OUTPUT_TOKENS = "Mean output tokens"
_AVERAGE_CACHED_INPUT_TOKENS = "Mean reported cached input tokens"
_GENERATION_TOKENS_PER_CONFIGURED_CONCURRENCY = (
    "Output tok/s / user"
)
_GENERATION_TOKENS_PER_GPU = "Output token throughput per GPU (tokens/s)"
_CONCURRENT_CONVERSATIONS = "Conversation concurrency limit"
_CONVERSATIONS = "Conversations attempted"
_CONVERSATIONS_PER_SECOND = "Attempted conversations per second"
_AVERAGE_TURNS_PER_CONVERSATION = "Mean turn requests per conversation"

_TRACE_MAX_BUCKETS = 10_000
_DISTRIBUTION_METRICS = (
    ("latency", "End-to-end latency (E2EL) (s)", 1.0),
    ("ttft", "TTFT (s)", 1.0),
    ("tpot", "TPOT (ms)", 1000.0),
    ("itl", "ITL (ms)", 1000.0),
    ("replay_delay", "Replay delay (s)", 1.0),
    ("trace_e2e_ttft", "TTFT including replay delay (s)", 1.0),
    ("trace_e2e_latency", "E2EL including replay delay (s)", 1.0),
)
_TRACE_DISTRIBUTION_METRICS = tuple(
    item for item in _DISTRIBUTION_METRICS if item[0] != "itl"
)
_TRACE_HISTORY_KEYS = {
    "requests_per_second": "Trace/Scheduled requests per second",
    "successful_requests_per_second": "Trace/Successful scheduled requests/s",
    **{
        key: f"Trace/{name} p95"
        for key, name, _ in _TRACE_DISTRIBUTION_METRICS
    },
}


def wandb_metric_fields(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map final benchmark metrics to existing W&B chart fields."""
    throughput = metrics["throughput"]
    message = {
        _TIME_TAKEN: round(float(metrics["benchmark_time"]), 4),
        _CONCURRENCY: int(metrics["max_concurrency"]),
        "Peak in-flight requests": metrics["request_concurrency"]["peak"],
        "Mean in-flight requests": metrics["request_concurrency"]["mean"],
        (_REQUEST_RATE if not metrics.get("multi_turn") else "Conversation arrival rate (conv/s)"): float(metrics["request_rate"]),
        _TOTAL_REQUESTS: int(metrics["request_num"]),
        _SUCCEED_REQUESTS: int(metrics["success_num"]),
        _FAILED_REQUESTS: int(metrics["failed_num"]),
        _SUCCESS_RATE: round(float(metrics["success_rate"]) * 100.0, 4),
        _REQUESTS_PER_SECOND: round(float(throughput["requests_per_second"]), 4),
    }
    for source, label in (
        ("total_output_tokens", "Total output tokens"),
        ("planned_num", "Prepared requests"),
        ("admitted_num", "Admitted requests"),
        ("not_executed_num", "Not executed requests"),
    ):
        if metrics.get(source) is not None:
            message[label] = metrics[source]
    for reason, count in metrics.get("not_executed_reasons", {}).items():
        message[f"Not executed/{reason}"] = count
    throughput_fields = (
        ("prompt_tokens_per_second", _INPUT_TOKENS_PER_SECOND),
        ("generation_tokens_per_second", _GENERATION_TOKENS_PER_SECOND),
        ("total_tokens_per_second", _TOTAL_TOKENS_PER_SECOND),
        (
            "generation_tokens_per_second_per_user",
            _GENERATION_TOKENS_PER_CONFIGURED_CONCURRENCY,
        ),
        ("generation_tokens_per_second_per_gpu", _GENERATION_TOKENS_PER_GPU),
    )
    for source, destination in throughput_fields:
        value = throughput.get(source)
        if value is not None:
            message[destination] = round(float(value), 4)
    optional = (
        ("avg_input_tokens", _AVERAGE_INPUT_TOKENS, 1.0, 4),
        ("avg_output_tokens", _AVERAGE_OUTPUT_TOKENS, 1.0, 4),
        (
            "avg_cached_input_tokens",
            _AVERAGE_CACHED_INPUT_TOKENS,
            1.0,
            4,
        ),
    )
    for source, destination, scale, digits in optional:
        value = metrics[source]
        if isinstance(value, dict):
            value = value["mean"]
        if value is not None:
            message[destination] = round(float(value) * scale, digits)
    for key, name, scale in _DISTRIBUTION_METRICS:
        stats = metrics.get(key)
        if not isinstance(stats, dict):
            continue
        for statistic in ("mean", "max", "p50", "p95", "p99"):
            value = stats.get(statistic)
            if value is not None:
                message[f"{name}/{statistic}"] = round(
                    float(value) * scale, 4
                )
        if key == "itl":
            for statistic, label in (
                ("count", "ITL/Interval count"),
                ("relative_error", "ITL/Quantile relative error"),
            ):
                value = stats.get(statistic)
                if value is not None:
                    message[label] = value
    slo = metrics.get("slo")
    if isinstance(slo, dict):
        for source, destination in (
            ("slo_attainment", "SLO attainment (%)"),
            ("request_goodput", "SLO request goodput (req/s)"),
            ("token_goodput", "SLO token goodput (tokens/s)"),
        ):
            value = slo.get(source)
            if value is not None:
                message[destination] = round(
                    float(value) * 100.0 if source == "slo_attainment" else float(value),
                    4,
                )
    allocation = metrics.get("gpu_allocation")
    if isinstance(allocation, dict):
        for resource, seconds in (allocation["gpu_seconds"] or {}).items():
            message[f"GPU allocation/{resource}/GPU-seconds"] = seconds
        for resource, hours in (allocation["gpu_hours"] or {}).items():
            message[f"GPU allocation/{resource}/GPU-hours"] = hours
        if allocation["coverage"] is not None:
            message["GPU allocation/Observation coverage (%)"] = allocation["coverage"] * 100
        if allocation["gpu_seconds"] is None:
            for resource, seconds in allocation["observed_gpu_seconds"].items():
                message[f"GPU allocation/{resource}/Observed GPU-seconds (partial)"] = seconds
    resources = metrics.get("resources")
    if isinstance(resources, dict):
        for key, label, scale in (
            ("cpu_usage_cores", "Resources/Mean CPU usage (cores)", 1),
            ("cpu_throttled_periods_ratio", "Resources/Throttled CPU periods (%)", 100),
            ("cpu_throttled_seconds", "Resources/CPU throttled time (s)", 1),
        ):
            value = resources.get(key)
            if value is not None:
                message[label] = round(float(value) * scale, 4)
    speculative = metrics.get("speculative_decoding")
    if isinstance(speculative, dict):
        for key, name, scale in (
            ("acceptance_ratio", "Speculative/Accepted draft tokens (%)", 100),
            ("accepted_tokens_per_draft", "Speculative/Accepted tokens per draft", 1),
            ("draft_mean_seconds", "Speculative/Draft GPU time per step (ms)", 1000),
            ("target_forward_mean_seconds", "Speculative/Target forward GPU time per step (ms)", 1000),
            ("draft_time_share_ratio", "Speculative/Draft share of measured GPU time (%)", 100),
            ("target_forward_time_share_ratio", "Speculative/Target forward share of measured GPU time (%)", 100),
        ):
            if speculative.get(key) is not None:
                message[name] = round(float(speculative[key]) * scale, 4)
    conversation = metrics.get("conversation")
    if isinstance(conversation, dict):
        message[_CONCURRENT_CONVERSATIONS] = int(metrics["max_concurrency"])
        message[_CONVERSATIONS] = int(conversation["attempted_num"])
        message[_CONVERSATIONS_PER_SECOND] = round(
            float(conversation["attempted_conversations_per_second"]), 4
        )
        message[_AVERAGE_TURNS_PER_CONVERSATION] = round(
            float(conversation["avg_turn_requests"]), 4
        )
    return message


def _trace_bucket_rows(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bucket by scheduled send time and build a p95 time series."""
    if not results:
        return []
    max_offset = max(float(result["trace_offset_s"]) for result in results)
    bucket_seconds = max(
        1.0,
        math.ceil((max_offset + 1.0) / _TRACE_MAX_BUCKETS),
    )
    buckets: dict[int, list[dict[str, Any]]] = {}
    for result in results:
        bucket = math.floor(
            float(result["trace_offset_s"]) / bucket_seconds
        )
        buckets.setdefault(bucket, []).append(result)

    rows: list[dict[str, Any]] = []
    for bucket in range(max(buckets) + 1):
        bucket_results = buckets.get(bucket, [])
        successful = [result for result in bucket_results if result["success"]]
        row: dict[str, Any] = {
            TRACE_TIME: bucket * bucket_seconds,
            "requests_per_second": len(bucket_results) / bucket_seconds,
            "successful_requests_per_second": len(successful) / bucket_seconds,
        }
        for key, _, scale in _TRACE_DISTRIBUTION_METRICS:
            values = [
                float(result[key])
                for result in successful
                if result.get(key) is not None
            ]
            value = percentile_summary(values)["p95"]
            if value is not None:
                row[key] = round(float(value) * scale, 4)
        rows.append(row)
    return rows


def publish_http_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish one completed HTTP benchmark to an already-open W&B SDK run."""
    if run.metrics.get("failure_reasons"):
        sdk_run.log({"Request failure reasons": wandb.Table(
            columns=["HTTP status", "Error", "Requests"],
            data=[[row["status_code"], row["error"], row["request_num"]]
                  for row in run.metrics["failure_reasons"]],
        )})
    replica_observations = None
    replica_path = run.artifacts.get("replica_observations")
    if replica_path is not None:
        replica_observations = json.loads(replica_path.read_text(encoding="utf-8"))
    allocation_path = run.artifacts.get("gpu_allocation")
    allocation = (
        json.loads(allocation_path.read_text(encoding="utf-8"))
        if allocation_path is not None else None
    )

    if run.measurements is not None:
        define_axis_metrics(sdk_run, ELAPSED_TIME)
        define_axis_metrics(sdk_run, REQUEST_INDEX)
        if allocation is not None:
            resources = {resource for sample in allocation["samples"] for resource in sample["gpu_counts"]}
            for resource in sorted(resources):
                sdk_run.define_metric(f"Resources/{resource}/*", step_metric=ELAPSED_TIME, step_sync=False)
        request_slo_met = (run.metrics.get("slo") or {}).get("request_slo_met")
        elapsed_rows = [
            *(time_series(
                run.measurements,
                duration=float(run.metrics["benchmark_time"]),
                stream=bool(run.metrics["stream"]),
                slo_met=request_slo_met,
            ) if not run.http_series_published else ()),
            *(cumulative_series(
                run.measurements,
                stream=bool(run.metrics["stream"]),
            ) if not run.http_series_published else ()),
        ]
        for field, group_name in (("dataset", "datasets"), ("model", "models"), ("request_class", "request_classes")):
            groups = run.metrics.get(group_name) or {}
            if not groups:
                continue
            labels = {name: f"{group_name}/{name}" for name in groups}
            for name, label in labels.items():
                selected = [
                    (index, item) for index, item in enumerate(run.measurements)
                    if getattr(item, field) == name
                ]
                subset = [item for _, item in selected]
                subset_slo = (
                    [request_slo_met[index] for index, _ in selected]
                    if request_slo_met is not None else None
                )
                for row in time_series(
                    subset, duration=float(run.metrics["benchmark_time"]),
                    stream=bool(run.metrics["stream"]), slo_met=subset_slo,
                ):
                    elapsed_rows.append({
                        ELAPSED_TIME: row[ELAPSED_TIME],
                        **{f"{label}/{key.removeprefix('Time/')}": value
                           for key, value in row.items() if key != ELAPSED_TIME},
                    })
            sdk_run.log({f"Breakdown/{group_name}": wandb.Table(
                columns=["Name", "Requests", "Success", "Request throughput (req/s)",
                         "Total output tokens", "Output tokens/s", "P95 E2EL (s)", "SLO attainment (%)", "Goodput (req/s)"],
                data=[[
                    name, group["request_num"], group["success_num"],
                    group["throughput"]["requests_per_second"],
                    group.get("total_output_tokens"),
                    group["throughput"]["generation_tokens_per_second"],
                    group["latency"]["p95"],
                    (group.get("slo") or {}).get("slo_attainment") * 100
                    if (group.get("slo") or {}).get("slo_attainment") is not None else None,
                    (group.get("slo") or {}).get("request_goodput"),
                ] for name, group in groups.items()],
            )})
        if replica_observations:
            elapsed_rows.extend(replica_history_rows(replica_observations))
        if allocation is not None:
            elapsed_rows.extend(gpu_allocation_history_rows(allocation))
        elapsed_rows.sort(key=lambda row: float(row[ELAPSED_TIME]))
        series = (
            elapsed_rows,
            request_series(
                run.measurements,
                stream=bool(run.metrics["stream"]),
                slo_met=request_slo_met,
            ) if not run.http_series_published else (),
        )
        if any(run.metrics.get(name) for name in ("datasets", "models", "request_classes")) or any(
            item.target_output_tokens is not None for item in run.measurements
        ):
            sdk_run.log({"Breakdown/Requests": wandb.Table(
                columns=["Start (s)", "Dataset", "Model", "Request class", "Priority",
                         "Target output tokens", "Actual output tokens", "E2EL (s)", "Success", "SLO met"],
                data=[[
                    item.started_at, item.dataset, item.model, item.request_class,
                    item.priority, item.target_output_tokens, item.output_tokens,
                    item.latency, item.succeeded,
                    request_slo_met[index] if isinstance(request_slo_met, list) else None,
                ] for index, item in enumerate(run.measurements)],
            )})
        for rows in series:
            for row in rows:
                sdk_run.log(row)

    raw_output = run.artifacts.get("raw_output")
    if raw_output is not None and run.record["mode"] == "arrival_trace":
        rows = _trace_bucket_rows(read_request_records(raw_output.parent))
        define_axis_metrics(sdk_run, TRACE_TIME)
        for row in rows:
            message = {TRACE_TIME: row[TRACE_TIME]}
            message.update(
                {
                    wandb_key: row[key]
                    for key, wandb_key in _TRACE_HISTORY_KEYS.items()
                    if key in row
                }
            )
            sdk_run.log(message)

    sdk_run.log(wandb_metric_fields(run.metrics))
    if "prometheus_observations" in run.artifacts:
        from benchmarks.results.plots.charts.resources import _prometheus_charts

        for chart in _prometheus_charts(run.artifacts["prometheus_observations"].parent):
            if chart.metric.startswith("spec_"):
                group = "Speculative"
                title = f"{chart.ylabel} · 5-minute observation window"
            elif chart.metric.startswith(("cpu_", "memory_")):
                group = "Resources"
                title = chart.ylabel
            else:
                continue
            sdk_run.log({f"{group}/{chart.metric}": wandb.plot.line_series(
                xs=[list(series.x) for series in chart.series],
                ys=[list(series.y) for series in chart.series],
                keys=[series.name for series in chart.series],
                xname="Elapsed time (s)",
                title=title,
            )})


def publish_sweep_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish the shared repeat statistics and data-backed comparison curves."""
    from benchmarks.results.plots import sweep_charts

    points = read_record(run.artifacts["sweep_points"].parent, "sweep_points.json")
    summary = read_record(run.artifacts["sweep_summary"].parent, "sweep_summary.json")
    columns = ["method", "combination", "parameter_group", "bench", "metric", "requested_runs",
               "runs", "failed_runs", "samples", "mean", "stddev", "median", "min", "max"]
    sdk_run.log({"Sweep/Summary": wandb.Table(
        columns=columns,
        data=[[json.dumps(row[key], ensure_ascii=False) if key == "bench" else row[key] for key in columns] for row in summary],
        allow_mixed_types=True,
    )})
    for chart in sweep_charts(points, summary):
        if chart.tick_labels:
            table = wandb.Table(columns=["Method / condition", chart.ylabel], data=[
                [f"{series.name} / {chart.tick_labels[int(x)]}", y if math.isfinite(y) else None]
                for series in chart.series for x, y in zip(series.x, series.y)
            ])
            plot = wandb.plot.bar(table, "Method / condition", chart.ylabel, title=chart.title)
        else:
            plot = wandb.plot.line_series(
                xs=[list(series.x) for series in chart.series],
                ys=[[y if math.isfinite(y) else None for y in series.y] for series in chart.series],
                keys=[series.name for series in chart.series], xname=chart.xlabel,
                title=f"{chart.title} — {chart.ylabel}",
            )
        sdk_run.log({f"Sweep/{chart.name}": plot})
    sdk_run.summary.update({f"Sweep/{key}": run.metrics["sweep"][key]
                            for key in ("status", "tasks", "completed", "failed", "interrupted", "running", "pending")})


def publish_slo_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish configured limits, measured request peaks, and stop reasons for one search."""
    search = run.metrics["slo_search"]
    sdk_run.log({
        "SLO/Probes": wandb.Table(
            columns=[
                "Group", f"Concurrency limit ({search['concurrency_limit_unit']})",
                "Peak in-flight requests", "Peaks per repetition", "SLO met",
            ],
            data=[
                [row["group"], row["max_concurrency"], row["peak_request_concurrency"],
                 row["repeat_peak_request_concurrency"], row["satisfied"]]
                for row in search["probes"]
            ],
        ),
    })
    for group in search["groups"]:
        prefix = f"SLO/Group {group['group']}"
        sdk_run.summary.update({
            f"{prefix}/Best passing request peak": group["best_peak_request_concurrency"],
            f"{prefix}/Configured limit at best": group["best_max_concurrency"],
            f"{prefix}/Last request peak": group["last_peak_request_concurrency"],
            f"{prefix}/Last configured limit": group["last_max_concurrency"],
            f"{prefix}/Stop reason": group["stop_reason"],
        })
