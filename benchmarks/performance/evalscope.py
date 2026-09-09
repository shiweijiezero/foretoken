# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Map Foretoken standard HTTP workloads to EvalScope public benchmark APIs."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evalscope.perf.utils.perf_models import BenchmarkSummary, PercentileResult
    from evalscope.perf.utils.trace_metrics import TraceLevelSummary

from benchmarks.performance.config import HttpBenchmarkConfig
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint
from benchmarks.performance.conversation import (
    load_chat_conversations,
    split_chat_conversation,
)
from benchmarks.performance.metrics import generation_tokens_per_second_per_user


EVALSCOPE_API = "foretoken_openai"
_EVALSCOPE_ARGUMENTS_TYPE: type | None = None


def _evalscope_arguments_type() -> tuple[str, type]:
    """Load and register the Foretoken EvalScope adapter once when a load runs."""
    global _EVALSCOPE_ARGUMENTS_TYPE
    if _EVALSCOPE_ARGUMENTS_TYPE is not None:
        return EVALSCOPE_API, _EVALSCOPE_ARGUMENTS_TYPE
    try:
        from pydantic import Field
        from evalscope.perf.arguments import Arguments
        from evalscope.perf.plugin.api.openai_api import OpenaiPlugin
        from evalscope.perf.plugin.registry import register_api
    except ModuleNotFoundError as error:
        raise ValueError(
            "standard HTTP loads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    class ForetokenEvalScopeArguments(Arguments):
        """EvalScope arguments carrying Foretoken request-field omission semantics."""

        omit_temperature: bool = Field(default=False, exclude=True, repr=False)

    @register_api(EVALSCOPE_API)
    class ForetokenOpenaiPlugin(OpenaiPlugin):
        """Reuse EvalScope OpenAI execution while omitting an unset temperature."""

        def build_request(self, messages: Any, param: Any = None) -> dict[str, Any]:
            """Build a request and restore Foretoken optional-temperature semantics."""
            request = super().build_request(messages, param)
            if self.param.omit_temperature:
                request.pop("temperature", None)
            return request

    _EVALSCOPE_ARGUMENTS_TYPE = ForetokenEvalScopeArguments
    return EVALSCOPE_API, ForetokenEvalScopeArguments


def _materialize_evalscope_request_dataset(
    benchmark: HttpBenchmarkConfig,
    output_dir: str,
) -> tuple[str, str]:
    """Normalize conversations and select the EvalScope single-turn or multi-turn plugin."""
    conversations = load_chat_conversations(benchmark)
    turn_lists = [split_chat_conversation(messages) for messages in conversations]
    effective_turn_lists = [
        turns[: benchmark.request_dataset.max_turns]
        if benchmark.request_dataset.max_turns is not None
        and benchmark.request_dataset.max_turns > 0
        else turns
        for turns in turn_lists
    ]
    dataset_name = (
        "custom_multi_turn"
        if any(len(turns) > 1 for turns in effective_turn_lists)
        else "line_by_line"
    )
    path = Path(output_dir) / "request_dataset.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for messages, turns in zip(conversations, effective_turn_lists):
            if dataset_name == "line_by_line":
                json.dump({"messages": turns[0]}, file, ensure_ascii=False)
            else:
                json.dump(messages, file, ensure_ascii=False)
            file.write("\n")
    return str(path), dataset_name


def _evalscope_arguments(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    output_dir: str,
) -> Any:
    """Map one Foretoken standard workload to EvalScope point arguments."""
    EVALSCOPE_API, ForetokenEvalScopeArguments = _evalscope_arguments_type()

    schedule = benchmark.load_schedule
    generation = benchmark.generation
    dataset = benchmark.request_dataset
    dataset_path, dataset_name = _materialize_evalscope_request_dataset(
        benchmark, output_dir
    )
    if dataset_name == "custom_multi_turn" and (
        schedule.unbounded_concurrency or schedule.arrival_rate != -1
    ):
        Path(dataset_path).unlink(missing_ok=True)
        raise ValueError(
            "Multi-turn conversations require --rate -1 and no --open-loop; "
            "rate schedules independent requests"
        )
    omit_temperature = (
        generation.temperature is None
        and "temperature" not in generation.extra_body
    )
    argument_values: dict[str, Any] = {
        "model": endpoint.model,
        "url": endpoint.url,
        "api": EVALSCOPE_API,
        "api_key": benchmark.endpoint.api_key,
        "headers": endpoint.headers,
        "total_timeout": benchmark.endpoint.timeout_seconds,
        "read_timeout": benchmark.endpoint.timeout_seconds,
        "no_test_connection": True,
        "number": schedule.request_count,
        "parallel": schedule.max_concurrency,
        "rate": schedule.arrival_rate,
        "open_loop": schedule.unbounded_concurrency,
        "max_tokens": generation.max_tokens,
        "stream": generation.stream,
        "top_p": generation.top_p,
        "top_k": generation.top_k,
        # EvalScope 1.11.1 requires a float here.  The Foretoken plugin removes
        # this placeholder from the final request when no temperature was set.
        "temperature": (
            0.0 if generation.temperature is None else generation.temperature
        ),
        "frequency_penalty": generation.frequency_penalty,
        "repetition_penalty": generation.repetition_penalty,
        "extra_args": {
            **(
                {"min_p": generation.min_p}
                if generation.min_p is not None
                else {}
            ),
            **(
                {"presence_penalty": generation.presence_penalty}
                if generation.presence_penalty is not None
                else {}
            ),
            **generation.extra_body,
        }
        or None,
        "outputs_dir": output_dir,
        "no_timestamp": True,
        "name": "evalscope",
        "visualizer": None,
        "multi_turn": dataset_name == "custom_multi_turn",
        # EvalScope uses None for an unbounded custom conversation; -1 is
        # Foretoken's explicit complete-conversation spelling.
        "max_turns": (
            None
            if dataset_name == "line_by_line" or dataset.max_turns == -1
            else dataset.max_turns
        ),
        "omit_temperature": omit_temperature,
    }
    argument_values.update(
        {
            "dataset": dataset_name,
            "dataset_path": dataset_path,
            "dataset_offset": 0,
        }
    )
    return ForetokenEvalScopeArguments(**argument_values)


def _percentile_value(
    percentiles: PercentileResult,
    label: str,
    field: str,
    *,
    scale: float = 1.0,
) -> float | None:
    value = float(percentiles.get_p(label, field))
    return value * scale if math.isfinite(value) else None


def _metric_distribution(
    summary_value: float,
    percentiles: PercentileResult,
    field: str,
    *,
    scale: float = 1.0,
) -> dict[str, float | None]:
    mean = float(summary_value) * scale
    return {
        "mean": mean if math.isfinite(mean) and mean >= 0 else None,
        "p50": _percentile_value(percentiles, "50%", field, scale=scale),
        "p95": _percentile_value(percentiles, "95%", field, scale=scale),
        "p99": _percentile_value(percentiles, "99%", field, scale=scale),
    }


def _trace_metric_distribution(
    trace_summary: TraceLevelSummary | None,
    metric_name: str,
) -> dict[str, float | None]:
    """Read a conversation-level metric distribution from the EvalScope per-trace summary."""
    if trace_summary is not None:
        for row in trace_summary.rows:
            if row.metric == metric_name:
                return {
                    "mean": float(row.mean),
                    "p50": float(row.p50),
                    "p95": float(row.p95),
                    "p99": float(row.p99),
                }
    return {"mean": None, "p50": None, "p95": None, "p99": None}


def _map_evalscope_metrics(
    benchmark: HttpBenchmarkConfig,
    summary: BenchmarkSummary,
    percentiles: PercentileResult,
    trace_summary: TraceLevelSummary | None = None,
) -> dict[str, Any]:
    """Map typed EvalScope results to Foretoken metric fields."""
    schedule = benchmark.load_schedule
    reported_concurrency = (
        -1 if schedule.unbounded_concurrency else schedule.max_concurrency
    )
    if benchmark.generation.stream and summary.succeed_requests:
        ttft = _metric_distribution(
            summary.avg_ttft, percentiles, "ttft", scale=0.001
        )
        tpot = _metric_distribution(
            summary.avg_tpot, percentiles, "tpot", scale=0.001
        )
        itl = _metric_distribution(
            summary.avg_itl, percentiles, "itl", scale=0.001
        )
    else:
        empty = {"mean": None, "p50": None, "p95": None, "p99": None}
        ttft = dict(empty)
        tpot = dict(empty)
        itl = dict(empty)

    output_throughput = float(summary.output_token_throughput)
    total_throughput = float(summary.total_token_throughput)
    throughput = {
        "requests_per_second": float(summary.request_throughput),
        "generation_tokens_per_second": output_throughput,
        "prompt_tokens_per_second": max(
            0.0, total_throughput - output_throughput
        ),
        "total_tokens_per_second": total_throughput,
        "generation_tokens_per_second_per_user": generation_tokens_per_second_per_user(
            output_throughput,
            reported_concurrency,
        ),
    }
    metrics = {
        "request_num": int(summary.total_requests),
        "success_num": int(summary.succeed_requests),
        "failed_num": int(summary.failed_requests),
        "success_rate": (
            summary.succeed_requests / summary.total_requests
            if summary.total_requests
            else 0.0
        ),
        "stream": benchmark.generation.stream,
        "latency": _metric_distribution(
            summary.avg_latency, percentiles, "latency"
        ),
        "ttft": ttft,
        "tpot": tpot,
        "itl": itl,
        "throughput": throughput,
        "avg_input_tokens": (
            float(summary.avg_input_tokens)
            if summary.succeed_requests
            else None
        ),
        "avg_output_tokens": (
            float(summary.avg_output_tokens)
            if summary.succeed_requests
            else None
        ),
        "benchmark_time": float(summary.time_taken),
        "rate": float(schedule.arrival_rate),
        "number": int(schedule.request_count),
        "parallel": reported_concurrency,
    }
    if benchmark.is_multi_turn:
        metrics["multi_turn"] = True
        conversation_count = int(schedule.request_count)
        benchmark_time = float(summary.time_taken)
        throughput["attempted_conversations_per_second"] = (
            conversation_count / benchmark_time if benchmark_time > 0 else 0.0
        )
        metrics["conversation"] = {
            "attempted_num": conversation_count,
            "max_turns": benchmark.request_dataset.max_turns,
            "avg_turn_requests": (
                int(summary.total_requests) / conversation_count
                if conversation_count
                else 0.0
            ),
            "avg_context_turns_per_request": (
                float(summary.avg_turns)
                if summary.avg_turns is not None
                else None
            ),
            "latency": _trace_metric_distribution(
                trace_summary, "Latency (s)"
            ),
            "first_turn_ttft": _trace_metric_distribution(
                trace_summary if benchmark.generation.stream else None,
                "First-Turn TTFT (s)",
            ),
            "time_to_final_answer_token": _trace_metric_distribution(
                trace_summary if benchmark.generation.stream else None,
                "TTFAT (s)",
            ),
            "decode_tokens_per_second": _trace_metric_distribution(
                trace_summary if benchmark.generation.stream else None,
                "Decode TPS",
            ),
            "cache_hit_rate_percent": _trace_metric_distribution(
                trace_summary, "Cache Hit Rate (%)"
            ),
            "eligible_cache_hit_rate_percent": _trace_metric_distribution(
                trace_summary, "Eligible Cache Hit Rate (%)"
            ),
        }
    return metrics


def _read_evalscope_request_measurements(
    output_dir: str,
    total_time: float,
) -> dict[str, Any]:
    """Read EvalScope 1.11.1 SQLite records for multi-dataset merging.

    ``run_one_benchmark`` returns aggregate types only.  Multi-dataset runs
    need the per-request rows to combine success counts and latency samples,
    while single-dataset runs can publish EvalScope's database unchanged and
    avoid interpreting its storage schema.
    """
    database_path = os.path.join(output_dir, "benchmark_data.db")
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT success, is_stream, start_time, completed_time, latency,
                   first_chunk_latency, prompt_tokens, completion_tokens,
                   time_per_output_token, inter_token_latencies
            FROM result
            ORDER BY start_time
            """
        ).fetchall()
    if not rows:
        return {
            "results": [],
            "total_time": total_time,
            "local_artifact": "benchmark_data.db",
        }
    first_start = min(float(row[2]) for row in rows)
    results = []
    for row in rows:
        inter_token_latencies = json.loads(row[9] or "[]")
        results.append(
            {
                "success": bool(row[0]),
                "status_code": None,
                "stream": bool(row[1]),
                "latency": float(row[4] or 0.0),
                "ttft": (
                    float(row[5]) if row[5] is not None else None
                ),
                "tpot": (
                    float(row[8]) if row[8] is not None else None
                ),
                "itl_samples": inter_token_latencies,
                "input_tokens": int(row[6] or 0),
                "output_tokens": int(row[7] or 0),
                "error": None,
                "end_time": float(row[3]) - first_start,
            }
        )
    return {
        "results": results,
        "total_time": total_time,
        "local_artifact": "benchmark_data.db",
    }


async def run_evalscope_standard_load(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    output_dir: str,
    *,
    collect_request_measurements: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a standard workload through EvalScope public point execution and return mapped results."""
    try:
        from evalscope.perf.main import run_one_benchmark
        from evalscope.utils.logger import configure_logging
        from evalscope.utils.model_utils import seed_everything
    except ModuleNotFoundError as error:
        raise ValueError(
            "standard HTTP loads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    os.makedirs(output_dir, exist_ok=True)
    configure_logging(
        False,
        os.path.join(output_dir, "benchmark.log"),
    )
    arguments = _evalscope_arguments(benchmark, endpoint, output_dir)
    seed_everything(benchmark.request_dataset.random_seed)
    materialized_dataset = (
        arguments.dataset_path
        if arguments.dataset in {"line_by_line", "custom_multi_turn"}
        else None
    )
    try:
        result = await asyncio.to_thread(
            run_one_benchmark,
            arguments,
            output_dir,
        )
    finally:
        if materialized_dataset:
            Path(materialized_dataset).unlink(missing_ok=True)
    point = next(iter(result.values()))
    summary = point["metrics"]
    percentiles = point["percentiles"]
    trace_summary = point.get("trace_summary")
    metrics = _map_evalscope_metrics(
        benchmark, summary, percentiles, trace_summary
    )
    if collect_request_measurements:
        measurements = _read_evalscope_request_measurements(
            output_dir, float(summary.time_taken)
        )
    else:
        measurements = {
            "results": [],
            "total_time": float(summary.time_taken),
            "local_artifact": "benchmark_data.db",
        }
    return metrics, measurements
