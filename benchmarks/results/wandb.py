# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Write HTTP benchmark results to an independent Weights & Biases run."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import wandb

from benchmarks.results.metrics import percentile_summary

if TYPE_CHECKING:
    from benchmarks.results.output import BenchmarkRun
from benchmarks.results.replicas import replica_history_rows
from benchmarks.results.timeseries import (
    ELAPSED_TIME,
    REQUEST_INDEX,
    cumulative_series,
    request_series,
    time_series,
)


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
_CONCURRENT_CONVERSATIONS = "Concurrent conversations"
_CONVERSATIONS = "Conversations attempted"
_CONVERSATIONS_PER_SECOND = "Attempted conversations per second"
_AVERAGE_TURNS_PER_CONVERSATION = "Mean turn requests per conversation"
_CONVERSATION_LATENCY = "Conversation latency (s)"
_FINAL_ANSWER_TTFT = "Time to final-answer token (TTFAT) (s)"

_TRACE_MAX_BUCKETS = 10_000
_TRACE_TIME = "Scheduled trace time (s)"
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
        _REQUEST_RATE: float(metrics["request_rate"]),
        _TOTAL_REQUESTS: int(metrics["request_num"]),
        _SUCCEED_REQUESTS: int(metrics["success_num"]),
        _FAILED_REQUESTS: int(metrics["failed_num"]),
        _SUCCESS_RATE: round(float(metrics["success_rate"]) * 100.0, 4),
        _REQUESTS_PER_SECOND: round(float(throughput["requests_per_second"]), 4),
    }
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
        for percentile, value in stats.items():
            if value is not None:
                message[f"{name}/{percentile}"] = round(
                    float(value) * scale, 4
                )
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
        for key, name in (
            ("latency", _CONVERSATION_LATENCY),
            ("time_to_final_answer_token", _FINAL_ANSWER_TTFT),
        ):
            if key not in conversation:
                continue
            for percentile, value in conversation[key].items():
                if value is not None:
                    message[f"{name}/{percentile}"] = round(float(value), 4)
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
            _TRACE_TIME: bucket * bucket_seconds,
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
    replica_observations = None
    replica_path = run.artifacts.get("replica_observations")
    if replica_path is not None:
        replica_observations = json.loads(replica_path.read_text(encoding="utf-8"))

    if run.measurements is not None:
        sdk_run.define_metric(ELAPSED_TIME)
        sdk_run.define_metric(REQUEST_INDEX)
        elapsed_rows = [
            *time_series(
                run.measurements,
                duration=float(run.metrics["benchmark_time"]),
                stream=bool(run.metrics["stream"]),
            ),
            *cumulative_series(
                run.measurements,
                stream=bool(run.metrics["stream"]),
            ),
        ]
        if replica_observations:
            elapsed_rows.extend(replica_history_rows(replica_observations))
        elapsed_rows.sort(key=lambda row: float(row[ELAPSED_TIME]))
        series = (
            (ELAPSED_TIME, elapsed_rows),
            (
                REQUEST_INDEX,
                request_series(
                    run.measurements,
                    stream=bool(run.metrics["stream"]),
                    slo_met=(run.metrics.get("slo") or {}).get("request_slo_met"),
                ),
            ),
        )
        for axis, rows in series:
            defined = {axis}
            for row in rows:
                for key in row.keys() - defined:
                    sdk_run.define_metric(key, step_metric=axis, step_sync=False)
                    defined.add(key)
                sdk_run.log(row)

    raw_output = run.artifacts.get("raw_output")
    if raw_output is not None:
        rows = _trace_bucket_rows(
            json.loads(raw_output.read_text(encoding="utf-8"))
        )
        sdk_run.define_metric(_TRACE_TIME)
        for wandb_key in _TRACE_HISTORY_KEYS.values():
            sdk_run.define_metric(wandb_key, step_metric=_TRACE_TIME)
        for row in rows:
            message = {_TRACE_TIME: row[_TRACE_TIME]}
            message.update(
                {
                    wandb_key: row[key]
                    for key, wandb_key in _TRACE_HISTORY_KEYS.items()
                    if key in row
                }
            )
            sdk_run.log(message)

    prometheus_path = run.artifacts.get("prometheus_observations")
    if prometheus_path is not None:
        artifact = wandb.Artifact("benchmark-observations", type="benchmark")
        artifact.add_file(str(prometheus_path), name=prometheus_path.name)
        sdk_run.log_artifact(artifact)

    sdk_run.log(wandb_metric_fields(run.metrics))
