# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Map Foretoken generated HTTP workloads to EvalScope public benchmark APIs."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evalscope.perf.utils.perf_models import BenchmarkSummary, PercentileResult
    from evalscope.perf.utils.trace_metrics import TraceLevelSummary

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import (
    RequestMeasurement,
    generation_tokens_per_second_per_user,
)
from benchmarks.datasets.conversations import (
    load_conversation_tasks,
    split_chat_conversation,
)
from benchmarks.datasets.huggingface import resolve_tokenizer_path


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
        from evalscope.perf.plugin.registry import register_api, register_dataset
        from evalscope.perf.plugin.datasets.base import DatasetPluginBase, Turn as EvalScopeTurn
    except ModuleNotFoundError as error:
        raise ValueError(
            "standard HTTP loads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    class ForetokenEvalScopeArguments(Arguments):
        """EvalScope arguments carrying Foretoken request-field omission semantics."""

        omit_temperature: bool = Field(default=False, exclude=True, repr=False)
        max_retries: int = Field(default=0, ge=0)
        output_length_range: tuple[int, int] | None = None

    @register_dataset("foretoken_conversations")
    class ForetokenConversationDataset(DatasetPluginBase):
        """Supply prepared turn deltas and request fields to EvalScope's existing strategies."""

        def build_messages(self) -> Any:
            """Yield single-request message lists or multi-turn deltas from prepared rows."""
            for line in self.dataset_line_by_line(self.query_parameters.dataset_path):
                row = json.loads(line)
                deltas = row["turns"]
                turns = []
                for index, messages in enumerate(deltas):
                    # Metadata travels with the conversation context and is stripped
                    # by the API adapter before sending any request to the service.
                    last_turn = index == len(deltas) - 1
                    marker = {
                        "role": "system",
                        "content": "",
                        "_foretoken_request": {"fields": row["fields"], "last_turn": last_turn},
                    }
                    turns.append(
                        EvalScopeTurn(messages=[marker, *messages], is_final=last_turn)
                    )
                yield turns if self.query_parameters.multi_turn else turns[0].messages

    @register_api(EVALSCOPE_API)
    class ForetokenOpenaiPlugin(OpenaiPlugin):
        """Adapt EvalScope requests to Foretoken's Chat Completions semantics."""

        def build_request(self, messages: Any, param: Any = None) -> dict[str, Any]:
            """Wrap random text as one user turn and omit an unset temperature."""
            effective_param = param or self.param
            if isinstance(messages, str) and not effective_param.tokenize_prompt:
                messages = [{"role": "user", "content": messages}]
            metadata = None
            if isinstance(messages, list):
                markers = [
                    message["_foretoken_request"]
                    for message in messages
                    if isinstance(message, dict) and "_foretoken_request" in message
                ]
                if markers:
                    metadata = markers[-1]
                    messages = [message for message in messages if "_foretoken_request" not in message]
            request = super().build_request(messages, effective_param)
            if metadata is not None:
                for key, value in metadata["fields"].items():
                    request.setdefault(key, value)
                request["_foretoken_last_turn"] = metadata["last_turn"]
            if effective_param.omit_temperature:
                request.pop("temperature", None)
            if effective_param.output_length_range is not None:
                target = random.randint(*effective_param.output_length_range)
                request.update(max_tokens=target, min_tokens=target, ignore_eos=True)
            return request

        async def process_request(
            self, client_session: Any, url: str, headers: dict, body: dict
        ) -> Any:
            """Retry transient failures before any response content, counting wait time in latency."""
            last_turn = body.pop("_foretoken_last_turn", True)
            started_at = time.perf_counter()
            for attempt in range(self.param.max_retries + 1):
                result = await super().process_request(client_session, url, headers, body)
                status = result.status_code
                transport_error = status is None and any(
                    name in (result.error or "")
                    for name in ("aiohttp.client_exceptions.", "TimeoutError", "ConnectionResetError")
                )
                retryable = transport_error or (
                    status is not None and (status in (408, 409, 429) or status >= 500)
                )
                if (
                    result.success or result.response_messages
                    or not retryable or attempt == self.param.max_retries
                ):
                    break
                await asyncio.sleep(min(0.5 * 2 ** min(attempt, 4), 8.0))
            http_status = 200 if result.success else result.status_code
            if result.success and not last_turn and any(
                choice.get("finish_reason") in ("tool_calls", "function_call")
                or (choice.get("message") or choice.get("delta") or {}).get("tool_calls")
                for response in result.response_messages if isinstance(response, dict)
                for choice in response.get("choices", [])
            ):
                result.success = False
                result.error = "Model requested tool execution before the next turn; a harness is required"
            if result.success and self.param.output_length_range is not None:
                actual = result.completion_tokens
                expected = body["max_tokens"]
                if actual != expected:
                    result.success = False
                    result.error = (
                        f"Output length mismatch: requested {expected} tokens, service reported {actual}; "
                        "verify min_tokens and ignore_eos support"
                    )
            if attempt:
                elapsed_before_attempt = result.start_time - started_at
                result.start_time = started_at
                result.query_latency += elapsed_before_attempt
                if result.is_stream and result.first_chunk_latency:
                    result.first_chunk_latency += elapsed_before_attempt
            # EvalScope's SQLite schema omits status and error details. Keep
            # these per logical request so the exported JSON can retain them.
            completed_at = result.completed_time or time.perf_counter()
            diagnostic = {
                "start_time": result.start_time,
                "latency": completed_at - result.start_time,
                "status_code": http_status,
                "error": None if result.success else result.error,
            }
            diagnostics_path = Path(self.param.outputs_dir) / "request_diagnostics.jsonl"
            with diagnostics_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
            return result

    _EVALSCOPE_ARGUMENTS_TYPE = ForetokenEvalScopeArguments
    return EVALSCOPE_API, ForetokenEvalScopeArguments


def _materialize_evalscope_request_dataset(
    benchmark: BenchmarkConfig,
    output_dir: str,
) -> tuple[str, bool]:
    """Prepare complete tool-aware turn deltas and report whether they need multi-turn workers."""
    tasks = load_conversation_tasks(benchmark)
    turn_lists = [split_chat_conversation(task.messages()) for task in tasks]
    max_turns = benchmark.resolved_workload.max_turns
    effective_turn_lists = [
        turns[:max_turns] if max_turns is not None and max_turns > 0 else turns
        for turns in turn_lists
    ]
    multi_turn = any(len(turns) > 1 for turns in effective_turn_lists)
    path = Path(output_dir) / "request_dataset.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for task, turns in zip(tasks, effective_turn_lists):
            json.dump({"turns": turns, "fields": dict(task.metadata)}, file, ensure_ascii=False)
            file.write("\n")
    return str(path), multi_turn


def _evalscope_arguments(
    benchmark: BenchmarkConfig,
    service: ModelService,
    output_dir: str,
) -> Any:
    """Map one Foretoken generated workload to EvalScope point arguments."""
    EVALSCOPE_API, ForetokenEvalScopeArguments = _evalscope_arguments_type()

    schedule = benchmark.load
    generation = benchmark.generation
    dataset = benchmark.resolved_workload
    is_random = dataset.dataset_selectors == ["random"]
    omit_temperature = (
        generation.temperature is None
        and "temperature" not in generation.extra_body
    )
    argument_values: dict[str, Any] = {
        "model": service.model,
        "url": service.chat_completions_url,
        "api": EVALSCOPE_API,
        "api_key": service.api_key,
        "headers": service.request_headers,
        "max_retries": benchmark.service.max_retries,
        "total_timeout": benchmark.service.timeout_seconds,
        "read_timeout": benchmark.service.timeout_seconds,
        "no_test_connection": True,
        "number": schedule.request_count,
        # With no pacing, the finite request budget is also the maximum
        # possible concurrency; reuse EvalScope's existing worker scheduler.
        "parallel": (
            schedule.request_count if schedule.max_concurrency == -1 else schedule.max_concurrency
        ),
        "rate": schedule.arrival_rate,
        "open_loop": schedule.max_concurrency == -1 and schedule.arrival_rate > 0,
        "max_tokens": generation.max_tokens,
        "output_length_range": (
            (generation.min_output_length, generation.max_output_length)
            if generation.min_output_length is not None else None
        ),
        "stream": generation.stream,
        "top_p": generation.top_p,
        "top_k": generation.top_k,
        # EvalScope 1.11.1 requires a float here. The Foretoken plugin removes
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
        "omit_temperature": omit_temperature,
    }
    if is_random:
        argument_values.update(
            {
                "dataset": "random",
                "dataset_offset": dataset.row_offset,
                "tokenizer_path": resolve_tokenizer_path(dataset.tokenizer),
                "min_prompt_length": dataset.minimum_prompt_tokens,
                "max_prompt_length": dataset.maximum_prompt_tokens,
                "prefix_length": dataset.shared_prefix_tokens,
                # The upstream dataset accounts for chat framing when requested.
                "apply_chat_template": dataset.apply_chat_template,
                "multi_turn": False,
                "max_turns": None,
            }
        )
    else:
        dataset_path, multi_turn = _materialize_evalscope_request_dataset(
            benchmark, output_dir
        )
        if multi_turn and schedule.arrival_rate != -1:
            Path(dataset_path).unlink(missing_ok=True)
            raise ValueError(
                "Multi-turn conversations require --rate -1; "
                "rate schedules independent requests"
            )
        argument_values.update(
            {
                "dataset": "foretoken_conversations",
                "dataset_path": dataset_path,
                "dataset_offset": 0,
                "multi_turn": multi_turn,
                # EvalScope uses None for an unbounded custom conversation; -1
                # is Foretoken's explicit complete-conversation spelling.
                "max_turns": (
                    None
                    if not multi_turn or dataset.max_turns == -1
                    else dataset.max_turns
                ),
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
    benchmark: BenchmarkConfig,
    summary: BenchmarkSummary,
    percentiles: PercentileResult,
    trace_summary: TraceLevelSummary | None = None,
    *,
    single_turn: bool,
) -> dict[str, Any]:
    """Map typed EvalScope results to Foretoken metric fields."""
    schedule = benchmark.load
    reported_concurrency = schedule.max_concurrency
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
            "max_turns": benchmark.resolved_workload.max_turns,
            "avg_turn_requests": (
                int(summary.total_requests) / conversation_count
                if conversation_count
                else 0.0
            ),
            "avg_context_turns_per_request": (
                (1.0 if single_turn else float(summary.avg_turns))
                if summary.succeed_requests and (
                    single_turn or (summary.avg_turns is not None and summary.avg_turns >= 0)
                )
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
        if single_turn:
            # One-turn conversations have exactly the same timing samples as
            # their HTTP requests; no conversation trace summary is needed.
            metrics["conversation"]["latency"] = dict(metrics["latency"])
            metrics["conversation"]["first_turn_ttft"] = dict(ttft)
            metrics["conversation"]["time_to_final_answer_token"] = dict(ttft)
    return metrics


def _read_evalscope_request_measurements(output_dir: str) -> list[RequestMeasurement]:
    """Read EvalScope 1.11.1 SQLite records as per-request measurements.

    ``run_one_benchmark`` returns aggregate types only; the ``result`` table holds
    the per-request rows that multi-dataset runs merge. EvalScope persists HTTP
    turns without conversation identity, so conversation fields stay unset.
    """
    diagnostics_path = Path(output_dir) / "request_diagnostics.jsonl"
    diagnostics = {}
    if diagnostics_path.exists():
        with diagnostics_path.open(encoding="utf-8") as file:
            for line in file:
                item = json.loads(line)
                diagnostics[float(item["start_time"])] = item
    database_path = os.path.join(output_dir, "benchmark_data.db")
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT success, start_time, latency, first_chunk_latency,
                   prompt_tokens, completion_tokens, time_per_output_token,
                   inter_token_latencies
            FROM result
            ORDER BY start_time
            """
        ).fetchall()
    if not rows:
        return []
    first_start = min(float(row[1]) for row in rows)
    return [
        RequestMeasurement(
            started_at=float(row[1]) - first_start,
            ttft=float(row[3]) if row[3] is not None else None,
            latency=float(row[2] if row[2] is not None else diagnostics.get(float(row[1]), {}).get("latency", 0.0)),
            tpot=float(row[6]) if row[6] is not None else None,
            itl_samples=tuple(float(value) for value in json.loads(row[7] or "[]")),
            input_tokens=int(row[4] or 0),
            output_tokens=int(row[5] or 0),
            succeeded=bool(row[0]),
            conversation_id=None,
            turn=None,
            status_code=diagnostics.get(float(row[1]), {}).get("status_code"),
            error_message=diagnostics.get(float(row[1]), {}).get("error"),
        )
        for row in rows
    ]


def run_evalscope_standard_load(
    benchmark: BenchmarkConfig,
    service: ModelService,
    output_dir: str,
) -> tuple[dict[str, Any], list[RequestMeasurement]]:
    """Run a generated workload through EvalScope and return its metrics and per-request measurements."""
    try:
        from evalscope.perf.main import run_one_benchmark
        from evalscope.perf.utils.handler import PerfBenchmarkInterrupted
        from evalscope.utils.logger import configure_logging
        from evalscope.utils.model_utils import seed_everything
    except ModuleNotFoundError as error:
        raise ValueError(
            "standard HTTP loads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    os.makedirs(output_dir, exist_ok=True)
    (Path(output_dir) / "request_diagnostics.jsonl").unlink(missing_ok=True)
    configure_logging(
        False,
        os.path.join(output_dir, "benchmark.log"),
    )
    arguments = _evalscope_arguments(benchmark, service, output_dir)
    seed_everything(benchmark.resolved_workload.random_seed)
    materialized_dataset = (
        arguments.dataset_path
        if arguments.dataset == "foretoken_conversations"
        else None
    )
    try:
        # EvalScope owns its event loop and signal cancellation on the main thread.
        result = run_one_benchmark(arguments, output_dir)
    except PerfBenchmarkInterrupted as error:
        raise SystemExit(error.exit_code) from None
    finally:
        if materialized_dataset:
            Path(materialized_dataset).unlink(missing_ok=True)
    point = next(iter(result.values()))
    summary = point["metrics"]
    percentiles = point["percentiles"]
    trace_summary = point.get("trace_summary")
    metrics = _map_evalscope_metrics(
        benchmark, summary, percentiles, trace_summary,
        single_turn=not arguments.multi_turn,
    )
    return metrics, _read_evalscope_request_measurements(output_dir)
