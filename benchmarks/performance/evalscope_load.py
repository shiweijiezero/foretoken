# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""把 Foretoken 标准 HTTP 负载映射到 EvalScope 公共性能接口。"""

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

from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.chat_client import ChatRequestContent
from benchmarks.performance.request_datasets import (
    load_chat_conversations,
    load_chat_requests,
)
from benchmarks.performance.request_metrics import (
    generation_tokens_per_second_per_user,
)


def uses_evalscope_standard_load(benchmark: HttpBenchmarkConfig) -> bool:
    """返回该标准负载是否符合 EvalScope 的公开负载参数契约。"""
    schedule = benchmark.load_schedule
    return not (
        schedule.unbounded_concurrency and schedule.arrival_rate == -1
    )


def _request_body(request: ChatRequestContent) -> dict[str, Any]:
    messages = request.messages
    if messages is None:
        if request.prompt is None:
            raise ValueError("Either prompt or messages must be provided")
        messages = [{"role": "user", "content": request.prompt}]
    body: dict[str, Any] = {"messages": messages}
    if request.tools:
        body["tools"] = request.tools
    return body


def _materialize_evalscope_request_dataset(
    benchmark: HttpBenchmarkConfig,
    output_dir: str,
) -> str:
    """把本地或 Hub 数据解析为 EvalScope 官方数据集插件输入。"""
    path = Path(output_dir) / "request_dataset.jsonl"
    with path.open("w", encoding="utf-8") as file:
        if benchmark.request_dataset.is_multi_turn:
            for messages in load_chat_conversations(benchmark):
                json.dump(messages, file, ensure_ascii=False)
                file.write("\n")
        else:
            for request in load_chat_requests(benchmark):
                json.dump(_request_body(request), file, ensure_ascii=False)
                file.write("\n")
    return str(path)


def _evalscope_arguments(
    benchmark: HttpBenchmarkConfig,
    output_dir: str,
) -> Any:
    """把一个 Foretoken 标准负载映射为 EvalScope 单点参数。"""
    try:
        from benchmarks.performance.evalscope_adapter import (
            EVALSCOPE_API,
            ForetokenEvalScopeArguments,
        )
    except ModuleNotFoundError as error:
        raise ValueError(
            "standard HTTP loads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    schedule = benchmark.load_schedule
    generation = benchmark.generation
    dataset = benchmark.request_dataset
    omit_temperature = (
        generation.temperature is None
        and "temperature" not in generation.extra_body
    )
    argument_values: dict[str, Any] = {
        "model": benchmark.endpoint.model,
        "url": benchmark.endpoint.url,
        "api": EVALSCOPE_API,
        "api_key": benchmark.endpoint.api_key,
        "headers": benchmark.endpoint.headers,
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
        "multi_turn": dataset.is_multi_turn,
        # EvalScope uses None for an unbounded custom conversation; -1 is
        # Foretoken's explicit complete-conversation spelling.
        "max_turns": None if dataset.max_turns == -1 else dataset.max_turns,
        "omit_temperature": omit_temperature,
    }
    if dataset.fixed_prompt:
        argument_values["prompt"] = dataset.fixed_prompt
    else:
        # Foretoken owns source selection and row normalization. EvalScope owns
        # either independent line-by-line requests or the interactive conversation
        # loop that appends each real model response before the next turn.
        argument_values.update(
            {
                "dataset": (
                    "custom_multi_turn" if dataset.is_multi_turn else "line_by_line"
                ),
                "dataset_path": _materialize_evalscope_request_dataset(
                    benchmark, output_dir
                ),
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
    """从 EvalScope per-trace 汇总读取一个对话级指标分布。"""
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
    """把 EvalScope 类型化结果映射到 Foretoken 当前指标字段。"""
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
    if benchmark.request_dataset.is_multi_turn:
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
    """读取 EvalScope 1.11.1 SQLite 记录，供多数据集合并使用。

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
    output_dir: str,
    *,
    collect_request_measurements: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """通过 EvalScope 公共单点入口执行标准负载并返回映射结果。"""
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
    arguments = _evalscope_arguments(benchmark, output_dir)
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
