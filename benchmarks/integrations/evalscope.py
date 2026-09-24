# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Map Foretoken generated HTTP workloads to EvalScope public benchmark APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import (
    ExitStack,
    asynccontextmanager,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from evalscope.perf.arguments import Arguments
from evalscope.perf.main import run_one_benchmark
from evalscope.perf.plugin.api.default_api import StreamedResponseHandler
from evalscope.perf.plugin.api.openai_api import OpenaiPlugin
from evalscope.perf.plugin.datasets.base import DatasetPluginBase, Turn as EvalScopeTurn
from evalscope.perf.plugin.registry import register_api, register_dataset
from evalscope.perf.utils.handler import PerfBenchmarkInterrupted
from evalscope.perf.utils.perf_models import BenchmarkSummary
from evalscope.perf.utils.trace_metrics import TraceLevelSummary
from evalscope.utils.logger import configure_logging, get_logger
from evalscope.utils.model_utils import seed_everything
from pydantic import Field

if TYPE_CHECKING:
    from benchmarks.profiling.capture import BenchmarkProfile

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.integrations.streaming import ChatStreamTiming
from benchmarks.results.metrics import (
    RequestMeasurement,
    compute_tpot,
    percentile_summary,
    summarize_measurements,
)
from benchmarks.datasets.conversations import (
    load_conversation_tasks,
    split_chat_conversation,
)
from benchmarks.datasets.huggingface import resolve_tokenizer_path


# Registry identities and private message fields shared by this adapter's
# dataset producer, API consumer, and argument mapping.
_EVALSCOPE_API: Final = "foretoken_openai"
_EVALSCOPE_DATASET: Final = "foretoken_conversations"
_REQUEST_METADATA: Final = "_foretoken_request"
_FINAL_TURN: Final = "_foretoken_last_turn"


class _TimedStreamResponse:
    """Expose EvalScope's streaming response surface while observing arriving bytes.

    The original response context owns the connection; this adapter neither
    consumes ahead nor changes the bytes delivered to the upstream decoder.
    """

    def __init__(self, response: Any, timing: ChatStreamTiming) -> None:
        self.status = response.status
        self.headers = response.headers
        self.content = self
        self._content = response.content
        self._timing = timing

    async def iter_any(self):
        """Timestamp complete SSE messages, preserving coalesced-message arrival times."""
        decoder = StreamedResponseHandler()
        async for data in self._content.iter_any():
            received_at = time.perf_counter()
            for message in decoder.add_chunk(data):
                payload = message.removeprefix("data:").strip()
                if payload != "[DONE]":
                    self._timing.observe(json.loads(payload), received_at)
            yield data


class _TimedClientSession:
    """Reuse the caller's aiohttp session and response cleanup for one measured attempt."""

    def __init__(self, session: Any, timing: ChatStreamTiming) -> None:
        self._session = session
        self._timing = timing

    @asynccontextmanager
    async def post(self, **kwargs: Any):
        """Wrap successful SSE responses; non-streaming and HTTP errors stay upstream-owned."""
        async with self._session.post(**kwargs) as response:
            if response.status == 200 and "text/event-stream" in response.headers.get("Content-Type", ""):
                yield _TimedStreamResponse(response, self._timing)
            else:
                yield response


@cache
def _evalscope_arguments_type() -> type:
    """Register the adapter lazily and reuse its argument type across sequential loads."""
    class ForetokenEvalScopeArguments(Arguments):
        """Carry Foretoken request semantics and an unpersisted capture handle into EvalScope."""

        omit_temperature: bool = Field(default=False, exclude=True, repr=False)
        max_retries: int = Field(ge=0)
        output_length_range: tuple[int, int] | None = None
        profile: Any = Field(default=None, exclude=True, repr=False)

    @register_dataset(_EVALSCOPE_DATASET)
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
                        _REQUEST_METADATA: {"fields": row["fields"], "last_turn": last_turn},
                    }
                    turns.append(
                        EvalScopeTurn(messages=[marker, *messages], is_final=last_turn)
                    )
                yield turns if self.query_parameters.multi_turn else turns[0].messages

    @register_api(_EVALSCOPE_API)
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
                    message[_REQUEST_METADATA]
                    for message in messages
                    if isinstance(message, dict) and _REQUEST_METADATA in message
                ]
                if markers:
                    metadata = markers[-1]
                    messages = [message for message in messages if _REQUEST_METADATA not in message]
            request = super().build_request(messages, effective_param)
            if metadata is not None:
                for key, value in metadata["fields"].items():
                    request.setdefault(key, value)
                request[_FINAL_TURN] = metadata["last_turn"]
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
            if self.param.profile is not None:
                await self.param.profile.before_request()
            last_turn = body.pop(_FINAL_TURN, True)
            started_at = time.perf_counter()
            for attempt in range(self.param.max_retries + 1):
                timing = ChatStreamTiming()
                result = await super().process_request(
                    _TimedClientSession(client_session, timing), url, headers, body
                )
                # Update the upstream observation before its metrics consumer calls
                # finalize(), so TPOT, SQLite, and conversation summaries agree.
                if result.is_stream and timing.first_output_at is not None:
                    result.first_chunk_latency = timing.first_output_at - result.start_time
                    result.inter_chunk_latency = timing.intervals
                    if result.success:
                        result.completed_time = timing.last_output_at
                        result.query_latency = result.completed_time - result.start_time
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
            has_tool_calls = any(
                choice.get("finish_reason") in ("tool_calls", "function_call")
                or (choice.get("message") or choice.get("delta") or {}).get("tool_calls")
                for response in result.response_messages if isinstance(response, dict)
                for choice in response.get("choices", [])
            )
            if result.success and not last_turn and has_tool_calls:
                result.success = False
                result.error = "Model requested tool execution before the next turn; a harness is required"
            reported_input = result.prompt_tokens
            reported_output = result.completion_tokens
            reported_cached = result.real_cached_tokens
            if result.success and self.param.output_length_range is not None:
                actual = reported_output
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
                "input_tokens": reported_input,
                "output_tokens": reported_output,
                "cached_input_tokens": reported_cached,
                "empty_stream": result.is_stream and timing.first_output_at is None,
            }
            diagnostics_path = Path(self.param.outputs_dir) / "request_diagnostics.jsonl"
            with diagnostics_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
            if self.param.profile is not None:
                self.param.profile.response_received(result.success)
            return result

    return ForetokenEvalScopeArguments


def _materialize_evalscope_request_dataset(
    benchmark: BenchmarkConfig,
    output_dir: str,
) -> tuple[str, bool, int]:
    """Materialize conversations without exceeding the configured request budget."""
    tasks = load_conversation_tasks(benchmark)
    request_budget = benchmark.load.request_count
    max_turns = benchmark.resolved_workload.max_turns
    effective_turn_lists: list[list[list[dict[str, Any]]]] = []
    remaining = request_budget
    for task in tasks:
        turns = [messages for messages, _ in split_chat_conversation(task.messages())]
        if max_turns is not None and max_turns > 0:
            turns = turns[:max_turns]
        if not turns or remaining <= 0:
            break
        selected = turns[:remaining]
        effective_turn_lists.append(selected)
        remaining -= len(selected)
    multi_turn = any(len(turns) > 1 for turns in effective_turn_lists)
    path = Path(output_dir) / "request_dataset.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for task, turns in zip(tasks, effective_turn_lists):
            json.dump(
                {"turns": turns, "fields": dict(task.metadata)},
                file,
                ensure_ascii=False,
            )
            file.write("\n")
    return str(path), multi_turn, len(effective_turn_lists)


def _evalscope_arguments(
    benchmark: BenchmarkConfig,
    service: ModelService,
    output_dir: str,
) -> Any:
    """Map one Foretoken generated workload to EvalScope point arguments."""
    ForetokenEvalScopeArguments = _evalscope_arguments_type()

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
        "tokenizer_path": (
            resolve_tokenizer_path(dataset.tokenizer)
            if dataset.tokenizer
            else None
        ),
        "api": _EVALSCOPE_API,
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
        "max_tokens": (
            generation.max_tokens if generation.min_output_length is None else None
        ),
        "output_length_range": (
            (generation.min_output_length, generation.max_output_length)
            if generation.min_output_length is not None else None
        ),
        "stream": generation.stream,
        "top_p": generation.top_p,
        "top_k": generation.top_k,
        # EvalScope requires a float here. The Foretoken plugin removes
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
        dataset_path, multi_turn, conversation_count = _materialize_evalscope_request_dataset(
            benchmark, output_dir
        )
        if multi_turn and schedule.arrival_rate != -1:
            Path(dataset_path).unlink(missing_ok=True)
            raise ValueError(
                "Multi-turn conversations require --request-rate -1; "
                "rate schedules independent requests"
            )
        argument_values.update(
            {
                "dataset": _EVALSCOPE_DATASET,
                "dataset_path": dataset_path,
                "dataset_offset": 0,
                # EvalScope's multi-turn scheduler counts conversations; the
                # materialized dataset already enforces Foretoken's request budget.
                "number": conversation_count if multi_turn else schedule.request_count,
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


def _conversation_metrics(
    benchmark: BenchmarkConfig,
    summary: BenchmarkSummary,
    trace_summary: TraceLevelSummary | None,
    conversation_count: int,
) -> dict[str, Any]:
    """Map EvalScope's multi-turn trace summary without duplicating request metrics."""
    benchmark_time = float(summary.time_taken)
    return {
        "attempted_num": conversation_count,
        "request_num": int(summary.total_requests),
        "max_turns": benchmark.resolved_workload.max_turns,
        "avg_turn_requests": (
            int(summary.total_requests) / conversation_count
            if conversation_count
            else 0.0
        ),
        "avg_context_turns_per_request": (
            float(summary.avg_turns)
            if summary.succeed_requests
            and summary.avg_turns is not None
            and summary.avg_turns >= 0
            else None
        ),
        "attempted_conversations_per_second": (
            conversation_count / benchmark_time if benchmark_time > 0 else 0.0
        ),
        "latency": _trace_metric_distribution(trace_summary, "Latency (s)"),
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
    }


def _read_evalscope_request_measurements(
    output_dir: str,
) -> tuple[list[RequestMeasurement], float | None]:
    """Read EvalScope SQLite records and their monotonic time origin.

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
                   inter_token_latencies, completed_time
            FROM result
            ORDER BY start_time
            """
        ).fetchall()
    if not rows:
        return [], None
    first_start = min(float(row[1]) for row in rows)
    measurements = []
    for row in rows:
        diagnostic = diagnostics.get(float(row[1]), {})
        latency = float(
            row[2]
            if row[2] is not None
            else diagnostic.get("latency", float(row[5]) - float(row[1]))
        )
        empty_stream = bool(diagnostic.get("empty_stream"))
        ttft = (
            float(row[3])
            if row[3] is not None and not empty_stream
            else None
        )
        output_tokens = diagnostic.get("output_tokens")
        measurements.append(
            RequestMeasurement(
                started_at=float(row[1]) - first_start,
                ttft=ttft,
                latency=latency,
                tpot=compute_tpot(latency, ttft, output_tokens),
                itl_samples=(
                    ()
                    if empty_stream
                    else tuple(float(value) for value in json.loads(row[4] or "[]"))
                ),
                input_tokens=diagnostic.get("input_tokens"),
                output_tokens=output_tokens,
                cached_input_tokens=diagnostic.get("cached_input_tokens"),
                succeeded=bool(row[0]),
                conversation_id=None,
                turn=None,
                status_code=diagnostic.get("status_code"),
                error_message=diagnostic.get("error"),
            )
        )
    return measurements, first_start


@contextmanager
def _evalscope_phase(
    label: str, work_items: int, *, quiet: bool, output_dir: str
) -> Iterator[None]:
    """Scope native progress output to one phase while keeping errors visible."""
    logger = get_logger()
    thread_id = threading.get_ident()

    def include_record(record: logging.LogRecord) -> bool:
        if record.thread != thread_id or record.levelno >= logging.ERROR:
            return True
        if record.levelno >= logging.WARNING:
            return record.funcName != "_log_warmup_handoff"
        # Foretoken owns user-visible configuration and result summaries. Keep
        # EvalScope's execution progress while removing its duplicate argument
        # dump and summary tables from this adapter's console output.
        return record.funcName not in {
            "_log_warmup_handoff",
            "run_one_benchmark",
            "statistic_benchmark_metric",
            "summary_result",
        }

    with ExitStack() as output:
        console_handlers = []
        error_handler = None
        if quiet:
            # EvalScope's progress bars write directly to stderr and retain
            # console handlers created before this phase. Route both to a log,
            # leaving the native file handler and request measurements unchanged.
            progress = output.enter_context(
                open(os.path.join(output_dir, "progress.log"), "a", encoding="utf-8")
            )
            for handler in logger.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(
                    handler, logging.FileHandler
                ):
                    console_handlers.append((handler, handler.stream))
                    handler.setStream(progress)
            error_handler = logging.StreamHandler(sys.stderr)
            error_handler.setLevel(logging.ERROR)
            logger.addHandler(error_handler)
            output.enter_context(redirect_stdout(progress))
            output.enter_context(redirect_stderr(progress))
        logger.addFilter(include_record)
        try:
            logger.info("%s: %d work items", label, work_items)
            yield
        finally:
            logger.removeFilter(include_record)
            if error_handler is not None:
                logger.removeHandler(error_handler)
                error_handler.close()
            for handler, stream in console_handlers:
                handler.setStream(stream)


def run_evalscope_standard_load(
    benchmark: BenchmarkConfig,
    service: ModelService,
    output_dir: str,
    *,
    phase_label: str,
    profile: BenchmarkProfile | None = None,
) -> tuple[dict[str, Any], list[RequestMeasurement], float | None]:
    """Run through EvalScope and return metrics, measurements, and their monotonic origin."""

    os.makedirs(output_dir, exist_ok=True)
    (Path(output_dir) / "request_diagnostics.jsonl").unlink(missing_ok=True)
    configure_logging(
        False,
        os.path.join(output_dir, "benchmark.log"),
    )
    with _evalscope_phase(
        phase_label,
        benchmark.load.request_count,
        quiet=benchmark.outputs.includes("quiet"),
        output_dir=output_dir,
    ):
        arguments = _evalscope_arguments(benchmark, service, output_dir)
        arguments.profile = profile
        seed_everything(benchmark.resolved_workload.random_seed)
        materialized_dataset = (
            arguments.dataset_path
            if arguments.dataset == _EVALSCOPE_DATASET
            else None
        )
        try:
            # EvalScope owns its event loop and signal cancellation on the main thread.
            result = run_one_benchmark(arguments, output_dir)
        except PerfBenchmarkInterrupted as error:
            raise SystemExit(error.exit_code) from None
        except asyncio.CancelledError:
            if profile is not None and profile.error is not None:
                raise profile.error from None
            raise
        finally:
            if materialized_dataset:
                Path(materialized_dataset).unlink(missing_ok=True)
    point = next(iter(result.values()))
    summary = point["metrics"]
    trace_summary = point.get("trace_summary")
    measurements, time_origin = _read_evalscope_request_measurements(output_dir)
    metrics = summarize_measurements(
        measurements,
        total_time=float(summary.time_taken),
        stream=benchmark.generation.stream,
        arrival_rate=float(benchmark.load.arrival_rate),
        request_count=int(benchmark.load.request_count),
        reported_concurrency=int(benchmark.load.max_concurrency),
        gpu_count=service.gpu_count,
        slo_criteria=(benchmark.slo.params[0] if benchmark.slo.params else None),
    )
    if arguments.multi_turn:
        metrics["conversation"] = _conversation_metrics(
            benchmark, summary, trace_summary, int(arguments.number)
        )
        if benchmark.generation.stream and any(
            item.succeeded and item.ttft is None for item in measurements
        ):
            # EvalScope's trace summary uses zero for streams without output
            # chunks. Keep those conversation timings unavailable instead.
            conversation = metrics["conversation"]
            for key in (
                "first_turn_ttft",
                "time_to_final_answer_token",
                "decode_tokens_per_second",
            ):
                conversation[key] = percentile_summary([])
    return metrics, measurements, time_origin
