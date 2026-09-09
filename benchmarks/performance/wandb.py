# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Write HTTP benchmark results to an independent Weights & Biases run."""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Any, Optional

import wandb

from benchmarks.performance.config import HttpBenchmarkConfig, WandbRunConfig
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint
from benchmarks.performance.metrics import percentile_summary

logger = logging.getLogger(__name__)

_SYSTEM_STATS_INTERVAL_S = 1.0
_TIME_TAKEN = "Benchmark duration (s)"
_CONCURRENCY = "Concurrency limit"
_REQUEST_RATE = "Arrival rate (req/s)"
_TOTAL_REQUESTS = "Requests"
_SUCCEED_REQUESTS = "Successful requests"
_FAILED_REQUESTS = "Failed requests"
_REQUESTS_PER_SECOND = "Requests per second"
_AVERAGE_LATENCY = "Mean latency (s)"
_AVERAGE_INPUT_TOKENS = "Mean input tokens"
_GENERATION_TOKENS_PER_SECOND = "Generation tokens per second (tokens/s)"
_TOTAL_TOKENS_PER_SECOND = "Total tokens per second (tokens/s)"
_AVERAGE_TTFT = "Mean TTFT (ms)"
_AVERAGE_TPOT = "Mean TPOT (ms)"
_AVERAGE_ITL = "Mean ITL (ms)"
_AVERAGE_OUTPUT_TOKENS = "Mean output tokens"
_CONCURRENT_CONVERSATIONS = "Concurrent conversations"
_CONVERSATIONS = "Conversations attempted"
_CONVERSATIONS_PER_SECOND = "Attempted conversations per second"
_AVERAGE_TURNS_PER_CONVERSATION = "Mean turn requests per conversation"
_CONVERSATION_LATENCY = "Conversation latency (s)"
_FINAL_ANSWER_TTFT = "Time to first token of final answer (s)"

_TRACE_MAX_BUCKETS = 10_000
_TRACE_TIME = "Scheduled trace time (s)"
_TRACE_PERCENTILE_METRICS = (
    ("latency", "Request latency (s)", 1.0),
    ("ttft", "Request TTFT (ms)", 1000.0),
    ("tpot", "TPOT (ms)", 1000.0),
    ("replay_delay", "Replay delay (s)", 1.0),
    ("trace_e2e_ttft", "End-to-end TTFT (ms)", 1000.0),
    ("trace_e2e_latency", "End-to-end latency (s)", 1.0),
)
_TRACE_HISTORY_KEYS = {
    "requests_per_second": "Trace/Scheduled requests per second",
    "successful_requests_per_second": "Trace/Successful requests per second",
    **{
        key: f"Trace/{name} p95"
        for key, name, _ in _TRACE_PERCENTILE_METRICS
    },
}


def wandb_run_timestamp() -> str:
    """Return a local timestamp for W&B names and grouping."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def wandb_group_name(
    config: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
) -> str:
    """Return the configured W&B name or a default name from the runtime model and timestamp."""
    run_name = config.wandb.run_name.strip()
    return run_name or f"{endpoint.model}_{wandb_run_timestamp()}"


def wandb_metric_fields(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map final benchmark metrics to existing W&B chart fields."""
    throughput = metrics["throughput"]
    message = {
        _TIME_TAKEN: round(float(metrics["benchmark_time"]), 4),
        _CONCURRENCY: int(metrics["parallel"]),
        _REQUEST_RATE: float(metrics["rate"]),
        _TOTAL_REQUESTS: int(metrics["request_num"]),
        _SUCCEED_REQUESTS: int(metrics["success_num"]),
        _FAILED_REQUESTS: int(metrics["failed_num"]),
        _REQUESTS_PER_SECOND: round(float(throughput["requests_per_second"]), 4),
        _GENERATION_TOKENS_PER_SECOND: round(
            float(throughput["generation_tokens_per_second"]), 4
        ),
        _TOTAL_TOKENS_PER_SECOND: round(
            float(throughput["total_tokens_per_second"]), 4
        ),
    }
    optional = (
        ("avg_input_tokens", _AVERAGE_INPUT_TOKENS, 1.0, 4),
        ("avg_output_tokens", _AVERAGE_OUTPUT_TOKENS, 1.0, 4),
        ("latency", _AVERAGE_LATENCY, 1.0, 4),
        ("ttft", _AVERAGE_TTFT, 1000.0, 2),
        ("tpot", _AVERAGE_TPOT, 1000.0, 2),
        ("itl", _AVERAGE_ITL, 1000.0, 2),
    )
    for source, destination, scale, digits in optional:
        value = metrics[source]
        if isinstance(value, dict):
            value = value["mean"]
        if value is not None:
            message[destination] = round(float(value) * scale, digits)
    for key, name, scale in _TRACE_PERCENTILE_METRICS:
        stats = metrics.get(key)
        if not isinstance(stats, dict):
            continue
        for percentile, value in stats.items():
            if value is not None:
                message[f"{name}/{percentile}"] = round(
                    float(value) * scale, 4
                )
    conversation = metrics.get("conversation")
    if isinstance(conversation, dict):
        message[_CONCURRENT_CONVERSATIONS] = int(metrics["parallel"])
        message[_CONVERSATIONS] = int(conversation["attempted_num"])
        message[_CONVERSATIONS_PER_SECOND] = round(
            float(throughput["attempted_conversations_per_second"]), 4
        )
        message[_AVERAGE_TURNS_PER_CONVERSATION] = round(
            float(conversation["avg_turn_requests"]), 4
        )
        for name, stats in (
            (_CONVERSATION_LATENCY, conversation["latency"]),
            (_FINAL_ANSWER_TTFT, conversation["time_to_final_answer_token"]),
        ):
            for percentile, value in stats.items():
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
        for key, _, scale in _TRACE_PERCENTILE_METRICS:
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


class WandbBenchmarkRun:
    """Own the optional W&B run lifecycle for one benchmark workload point."""

    def __init__(self) -> None:
        self._run: Optional[Any] = None

    def start(
        self,
        config: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
        *,
        output_dir: str,
        parallel: int,
        rate: float,
        name_suffix: Optional[str] = None,
        group: Optional[str] = None,
    ) -> None:
        """Create an independent run for the workload point when W&B output is enabled."""
        wandb_config: WandbRunConfig = config.wandb
        if not config.outputs.includes("wandb"):
            return

        os.makedirs(output_dir, exist_ok=True)
        base = group or wandb_group_name(config, endpoint)
        name = f"{base}_{name_suffix}" if name_suffix else base
        init_kwargs: dict[str, Any] = {
            "project": wandb_config.project,
            "name": name,
            "reinit": "create_new",
            "config": config.to_dict(),
            "dir": output_dir,
            "settings": wandb.Settings(
                x_stats_sampling_interval=_SYSTEM_STATS_INTERVAL_S
            ),
        }
        if group:
            init_kwargs["group"] = group
        if wandb_config.entity:
            init_kwargs["entity"] = wandb_config.entity
        try:
            self._run = wandb.init(**init_kwargs)
        except wandb.errors.Error as exc:
            logger.warning(
                "W&B unavailable; continuing with local results: %s",
                exc,
            )
            return
        logger.info(
            "W&B logging enabled: project=%s name=%s group=%s concurrency=%s rate=%s",
            wandb_config.project,
            name,
            group or "-",
            parallel,
            rate,
        )

    def log_metrics(self, metrics: dict[str, Any]) -> None:
        """Publish the final aggregated HTTP benchmark metrics."""
        if self._run is None:
            return
        message = wandb_metric_fields(metrics)
        if "replay_delay" in metrics:
            self._run.summary.update(message)
        else:
            self._run.log(message)

    def log_trace_measurements(self, results: list[dict[str, Any]]) -> None:
        """Upload trace history organized by scheduled time after replay completes."""
        if self._run is None:
            return
        rows = _trace_bucket_rows(results)
        try:
            self._run.define_metric(_TRACE_TIME)
            for wandb_key in _TRACE_HISTORY_KEYS.values():
                self._run.define_metric(wandb_key, step_metric=_TRACE_TIME)
            for row in rows:
                message = {_TRACE_TIME: row[_TRACE_TIME]}
                message.update(
                    {
                        wandb_key: row[key]
                        for key, wandb_key in _TRACE_HISTORY_KEYS.items()
                        if key in row
                    }
                )
                self._run.log(message)
        except wandb.errors.Error:
            logger.exception("Failed to upload W&B trace charts")
            return
        logger.info(
            "W&B trace charts uploaded: requests=%d buckets=%d",
            len(results),
            len(rows),
        )

    def finish(self) -> None:
        """Finish only the run owned by this object; leave other W&B runs in the process unchanged."""
        run = self._run
        self._run = None
        if run is not None:
            run.finish()
