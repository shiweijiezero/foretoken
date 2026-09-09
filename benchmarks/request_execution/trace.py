# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own trace parsing, payload binding, replay scheduling, and trace requests."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import httpx
from openai import APIError, AsyncOpenAI

from benchmarks.config import HttpBenchmarkConfig
from benchmarks.datasets.datasets import (
    ChatRequestContent,
    iter_dataset_rows,
    load_chat_requests,
    load_indexed_chat_requests,
)
from benchmarks.deployment import BenchmarkRuntimeEndpoint
from benchmarks.results.metrics import (
    compute_tpot,
    percentile_summary,
    summarize_http_measurements,
)
from benchmarks.results.publication import (
    ResultPublication,
    build_benchmark_run_record,
)
from benchmarks.datasets.synthetic import (
    create_trace_random_dataset_plugin,
    generate_trace_random_requests,
)

logger = logging.getLogger(__name__)


def _openai_base_url(chat_completions_url: str) -> str:
    return chat_completions_url.rstrip("/").removesuffix("/chat/completions")


class ChatCompletionsLoadClient:
    """Own the Chat Completions client and generation settings for one workload point."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
        *,
        max_connections: int,
    ) -> None:
        self._generation = benchmark.generation
        self._request_overrides = benchmark.generation.request_overrides()
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        # Each measured request must map to one service request; retries change arrival rate, failure rate, and latency.
        self._client = AsyncOpenAI(
            base_url=_openai_base_url(endpoint.url),
            api_key=benchmark.endpoint.api_key,
            max_retries=0,
            default_headers=endpoint.headers,
            http_client=httpx.AsyncClient(
                timeout=benchmark.endpoint.timeout_seconds,
                limits=limits,
            ),
        )
        self._model = endpoint.model

    async def __aenter__(self) -> ChatCompletionsLoadClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.close()

    async def send(self, request: ChatRequestContent) -> dict[str, Any]:
        """Send one independent chat request and return its benchmark observations."""
        messages = request.messages
        if messages is None:
            if request.prompt is None:
                raise ValueError("Either prompt or messages must be provided")
            messages = [{"role": "user", "content": request.prompt}]
        stream = self._generation.stream
        request_fields: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._generation.sample_max_tokens(),
            "stream": stream,
        }
        if self._request_overrides:
            request_fields["extra_body"] = self._request_overrides
        if stream:
            request_fields["stream_options"] = {"include_usage": True}
        if request.tools:
            request_fields["tools"] = request.tools

        started_at = time.perf_counter()
        ttft: Optional[float] = None
        input_tokens = output_tokens = 0
        status_code: Optional[int] = None
        error_message: Optional[str] = None
        success = True
        try:
            response = await self._client.chat.completions.create(**request_fields)
            status_code = httpx.codes.OK
            if stream:
                async for chunk in response:
                    if chunk.usage is not None:
                        input_tokens = int(chunk.usage.prompt_tokens)
                        output_tokens = int(chunk.usage.completion_tokens)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if (delta.content or delta.tool_calls) and ttft is None:
                        ttft = time.perf_counter() - started_at
            elif response.usage is not None:
                input_tokens = int(response.usage.prompt_tokens)
                output_tokens = int(response.usage.completion_tokens)
        except (APIError, httpx.HTTPError) as exc:
            success = False
            status_code = getattr(exc, "status_code", None)
            error_message = str(exc)

        latency = time.perf_counter() - started_at
        # TTFT and TPOT are defined only for streamed token arrivals.
        if not stream:
            ttft = None
        return {
            "success": success,
            "status_code": status_code,
            "stream": stream,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, output_tokens),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error_message,
        }


@dataclass(frozen=True)
class ArrivalTraceEvent:
    """Store a request arrival time, source row identity, and optional chat content."""

    timestamp_seconds: float
    source_row_index: int
    request: ChatRequestContent | None = None
    input_tokens: int | None = None
    hash_ids: list[int] | None = None
    conversation_id: str | None = None
    request_origin: str = ""


def _parse_studychat_event(
    row: object,
    *,
    dataset_path: Path,
    line_number: int,
    source_row_index: int,
) -> ArrivalTraceEvent:
    """Parse one StudyChat row while preserving its complete message context."""
    if not isinstance(row, dict):
        raise ValueError(f"Expected an object at {dataset_path}:{line_number}")

    required = ("timestamp", "chatId", "messages")
    missing = [name for name in required if name not in row]
    if missing:
        raise ValueError(
            f"Missing {', '.join(missing)} at {dataset_path}:{line_number}"
        )

    try:
        timestamp_ms = float(row["timestamp"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid timestamp at {dataset_path}:{line_number}"
        ) from error
    if not math.isfinite(timestamp_ms):
        raise ValueError(
            f"Timestamp must be finite at {dataset_path}:{line_number}"
        )

    conversation_id = row["chatId"]
    if conversation_id is None or not str(conversation_id):
        raise ValueError(f"Empty chatId at {dataset_path}:{line_number}")

    messages = row["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Invalid messages at {dataset_path}:{line_number}")

    input_tokens = row.get("input_length")
    if input_tokens is not None:
        try:
            input_tokens = int(input_tokens)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid input_length at {dataset_path}:{line_number}"
            ) from error
        if input_tokens <= 0:
            raise ValueError(
                f"Invalid input_length at {dataset_path}:{line_number}"
            )

    return ArrivalTraceEvent(
        timestamp_seconds=timestamp_ms / 1000.0,
        source_row_index=source_row_index,
        request=ChatRequestContent(messages=messages),
        input_tokens=input_tokens,
        conversation_id=str(conversation_id),
    )


def _parse_mooncake_event(
    row: object,
    *,
    dataset_path: Path,
    line_number: int,
    source_row_index: int,
) -> ArrivalTraceEvent:
    """Parse one Mooncake row without constructing request text at this stage."""
    if not isinstance(row, dict):
        raise ValueError(f"Expected an object at {dataset_path}:{line_number}")
    if "timestamp" not in row or "input_length" not in row:
        raise ValueError(
            "Mooncake trace needs timestamp and input_length at "
            f"{dataset_path}:{line_number}"
        )
    try:
        timestamp_ms = float(row["timestamp"])
        input_tokens = int(row["input_length"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid timestamp or input_length at {dataset_path}:{line_number}"
        ) from error
    if not math.isfinite(timestamp_ms) or input_tokens <= 0:
        raise ValueError(
            f"Invalid timestamp or input_length at {dataset_path}:{line_number}"
        )

    hash_ids = row.get("hash_ids")
    if hash_ids is not None:
        if not isinstance(hash_ids, list) or any(
            isinstance(hash_id, bool)
            or not isinstance(hash_id, int)
            or hash_id < 0
            for hash_id in hash_ids
        ):
            raise ValueError(f"Invalid hash_ids at {dataset_path}:{line_number}")

    conversation_id = row.get("chatId")
    return ArrivalTraceEvent(
        timestamp_seconds=timestamp_ms / 1000.0,
        source_row_index=source_row_index,
        input_tokens=input_tokens,
        hash_ids=hash_ids,
        conversation_id=(
            str(conversation_id) if conversation_id is not None else None
        ),
    )


TraceRowParser = Callable[..., ArrivalTraceEvent]
_TRACE_ROW_PARSERS: dict[str, TraceRowParser] = {
    "studychat": _parse_studychat_event,
    "mooncake": _parse_mooncake_event,
}


class ArrivalTraceReader:
    """Own trace resolution, format detection, time-window selection, and stable sorting."""

    def __init__(self, trace_selector: str | Path) -> None:
        self.trace_selector = str(trace_selector)
        self.trace_format: str | None = None

    def _iter_rows(self) -> Iterator[tuple[Path, int, int, Any]]:
        """Yield trace rows through the shared local and Hub source reader."""
        if not Path(self.trace_selector).expanduser().is_file():
            logger.info("Resolving trace source %s", self.trace_selector)
        yield from iter_dataset_rows(self.trace_selector)

    def _iter_events(self) -> Iterator[ArrivalTraceEvent]:
        row_parser: TraceRowParser | None = None
        for dataset_path, line_number, source_row_index, row in self._iter_rows():
            if row_parser is None:
                trace_format = self._detect_format(
                    row,
                    dataset_path=dataset_path,
                    line_number=line_number,
                )
                if self.trace_format not in (None, trace_format):
                    raise ValueError("Trace format changed between reads")
                self.trace_format = trace_format
                row_parser = _TRACE_ROW_PARSERS[trace_format]
            yield row_parser(
                row,
                dataset_path=dataset_path,
                line_number=line_number,
                source_row_index=source_row_index,
            )

    @staticmethod
    def _detect_format(
        row: object,
        *,
        dataset_path: Path,
        line_number: int,
    ) -> str:
        if isinstance(row, dict):
            if all(key in row for key in ("timestamp", "chatId", "messages")):
                return "studychat"
            if all(key in row for key in ("timestamp", "input_length")):
                return "mooncake"
        raise ValueError(
            f"Cannot detect trace format from {dataset_path}:{line_number}; "
            "expected StudyChat timestamp/chatId/messages or Mooncake "
            "timestamp/input_length"
        )

    def read_window(
        self,
        *,
        start_offset_seconds: float = 0.0,
        duration_seconds: float | None = None,
    ) -> tuple[float, list[ArrivalTraceEvent]]:
        """Select a half-open window relative to the first timestamp and sort by arrival time."""
        self.trace_format = None
        first_timestamp = None
        for event in self._iter_events():
            if first_timestamp is None:
                first_timestamp = event.timestamp_seconds
            else:
                first_timestamp = min(first_timestamp, event.timestamp_seconds)

        if first_timestamp is None:
            raise ValueError("Trace contains no requests")

        window_start = first_timestamp + start_offset_seconds
        window_end = (
            None if duration_seconds is None else window_start + duration_seconds
        )
        selected_events = [
            event
            for event in self._iter_events()
            if event.timestamp_seconds >= window_start
            and (window_end is None or event.timestamp_seconds < window_end)
        ]
        if not selected_events:
            raise ValueError("Trace window contains no requests")
        selected_events.sort(key=lambda event: event.timestamp_seconds)
        return window_start, selected_events


_MOONCAKE_BLOCK_TOKENS = 512


def generate_synthetic_prefix_reuse_requests(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    *,
    input_lengths: list[int],
    hash_id_lists: list[list[int] | None],
) -> list[ChatRequestContent]:
    """Build reproducible 512-token prefix blocks from Mooncake hash IDs."""
    dataset = benchmark.resolved_dataset
    if len(input_lengths) != len(hash_id_lists):
        raise ValueError("input_lengths must match hash_id_lists")

    plugin = create_trace_random_dataset_plugin(
        benchmark,
        endpoint,
        len(input_lengths),
    )
    tokenizer = plugin.tokenizer
    allowed_token_ids = [int(token_id) for token_id in plugin.allowed_tokens]

    @lru_cache(maxsize=1024)
    def block_for(hash_id: int) -> tuple[int, ...]:
        generator = random.Random(dataset.random_seed + hash_id)
        return tuple(
            generator.choice(allowed_token_ids)
            for _ in range(_MOONCAKE_BLOCK_TOKENS)
        )

    requests: list[ChatRequestContent] = []
    for input_length, hash_ids in zip(input_lengths, hash_id_lists):
        if hash_ids is None:
            raise ValueError(
                "--trace-synthetic-prefix-reuse requires hash_ids on every "
                "selected trace event"
            )
        expected_blocks = (
            input_length + _MOONCAKE_BLOCK_TOKENS - 1
        ) // _MOONCAKE_BLOCK_TOKENS
        if len(hash_ids) != expected_blocks:
            raise ValueError(
                "Mooncake hash_ids must cover every 512-token input block; "
                f"got {len(hash_ids)} hash_ids for input_length={input_length}"
            )

        prompt_token_ids = [
            token_id
            for hash_id in hash_ids
            for token_id in block_for(hash_id)
        ][:input_length]
        prompt = tokenizer.decode(
            prompt_token_ids,
            skip_special_tokens=False,
            clean_up_tokenization_spaces=False,
        )
        if not prompt:
            raise ValueError("Tokenizer produced an empty Mooncake payload")
        requests.append(ChatRequestContent(prompt=prompt))
    return requests


def _request_origin(benchmark: HttpBenchmarkConfig) -> str:
    return (
        "random"
        if benchmark.resolved_dataset.dataset_selectors == ["random"]
        else "dataset"
    )


def bind_arrival_trace_requests(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    events: list[ArrivalTraceEvent],
) -> tuple[str, list[ArrivalTraceEvent]]:
    """Bind native, random, or external-dataset requests to the selected arrival events."""
    request_origin = _request_origin(benchmark)
    dataset = benchmark.resolved_dataset
    trace = benchmark.arrival_trace
    if request_origin == "random":
        input_lengths = [event.input_tokens for event in events]
        has_input_lengths = [length is not None for length in input_lengths]
        if any(has_input_lengths) and not all(has_input_lengths):
            raise ValueError(
                "Random trace payload requires input_length on every "
                "selected trace event"
            )
        if trace.synthetic_prefix_reuse:
            if not all(has_input_lengths):
                raise ValueError(
                    "--trace-synthetic-prefix-reuse requires input_length "
                    "on every selected trace event"
                )
            requests = generate_synthetic_prefix_reuse_requests(
                benchmark,
                endpoint,
                input_lengths=[int(length) for length in input_lengths],
                hash_id_lists=[event.hash_ids for event in events],
            )
        else:
            requests = generate_trace_random_requests(
                benchmark,
                endpoint,
                request_count=len(events),
                input_lengths=(
                    [int(length) for length in input_lengths]
                    if all(has_input_lengths)
                    else None
                ),
            )
    elif dataset.dataset_selectors[0] == trace.trace_selector:
        if all(event.request is not None for event in events):
            return request_origin, [
                replace(event, request_origin=request_origin) for event in events
            ]
        requests = load_indexed_chat_requests(
            dataset.dataset_selectors[0],
            [event.source_row_index for event in events],
        )
    else:
        requests = load_chat_requests(benchmark, request_count=len(events))

    if len(requests) != len(events):
        raise ValueError(
            f"Loaded {len(requests)} requests for {len(events)} trace events"
        )

    return request_origin, [
        replace(event, request=request, request_origin=request_origin)
        for event, request in zip(events, requests)
    ]


class ArrivalTraceBenchmark:
    """Execute and summarize one timestamp-driven trace replay."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint

    async def _send_event(
        self,
        client: ChatCompletionsLoadClient,
        event: ArrivalTraceEvent,
        index: int,
        *,
        scheduled_at: float,
        trace_offset_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        actual_send_at = time.perf_counter()
        if event.request is None:
            raise RuntimeError("trace event has no bound chat request")
        measurement = await client.send(event.request)
        measurement["source_index"] = event.source_row_index
        measurement["conversation_id"] = event.conversation_id
        measurement["trace_timestamp_s"] = event.timestamp_seconds
        measurement["trace_offset_s"] = trace_offset_seconds
        measurement["trace_input_length"] = event.input_tokens
        measurement["trace_hash_block_count"] = (
            len(event.hash_ids) if event.hash_ids is not None else None
        )
        measurement["payload_source"] = event.request_origin
        replay_delay = max(0.0, actual_send_at - scheduled_at)
        measurement["replay_delay"] = replay_delay
        measurement["trace_e2e_latency"] = replay_delay + float(
            measurement["latency"]
        )
        if measurement["ttft"] is not None:
            measurement["trace_e2e_ttft"] = replay_delay + float(
                measurement["ttft"]
            )
        else:
            measurement["trace_e2e_ttft"] = None
        return index, measurement

    @staticmethod
    def _attach_replay_metrics(
        metrics: dict[str, Any],
        measurements: list[dict[str, Any]],
    ) -> None:
        successful = [item for item in measurements if item["success"]]
        for metric_name in (
            "replay_delay",
            "trace_e2e_ttft",
            "trace_e2e_latency",
        ):
            values = [
                float(item[metric_name])
                for item in successful
                if item[metric_name] is not None
            ]
            metrics[metric_name] = percentile_summary(values)

    async def _replay_events(
        self,
        client: ChatCompletionsLoadClient,
        events: list[ArrivalTraceEvent],
        *,
        max_concurrency: int | None,
        trace_window_start: float,
    ) -> dict[str, Any]:
        """Schedule requests by absolute recorded offset and include concurrency waits in replay delay."""
        completed_tasks: asyncio.Queue[
            asyncio.Task[tuple[int, dict[str, Any]]]
        ] = asyncio.Queue()
        pending_tasks: set[
            asyncio.Task[tuple[int, dict[str, Any]]]
        ] = set()
        measurements_by_index: dict[int, dict[str, Any]] = {}

        def collect(task: asyncio.Task[tuple[int, dict[str, Any]]]) -> None:
            pending_tasks.discard(task)
            index, measurement = task.result()
            measurements_by_index[index] = measurement

        def collect_ready_tasks() -> None:
            while True:
                try:
                    collect(completed_tasks.get_nowait())
                except asyncio.QueueEmpty:
                    return

        started_at = time.perf_counter()
        request_count = 0
        try:
            for event in events:
                scheduled_at = (
                    started_at
                    + event.timestamp_seconds
                    - trace_window_start
                )
                delay = scheduled_at - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

                collect_ready_tasks()
                while (
                    max_concurrency is not None
                    and len(pending_tasks) >= max_concurrency
                ):
                    collect(await completed_tasks.get())

                task = asyncio.create_task(
                    self._send_event(
                        client,
                        event,
                        request_count,
                        scheduled_at=scheduled_at,
                        trace_offset_seconds=(
                            event.timestamp_seconds - trace_window_start
                        ),
                    )
                )
                pending_tasks.add(task)
                task.add_done_callback(completed_tasks.put_nowait)
                request_count += 1

            while pending_tasks:
                collect(await completed_tasks.get())
        finally:
            if pending_tasks:
                for task in pending_tasks:
                    task.cancel()
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        return {
            "results": [
                measurements_by_index[index] for index in range(request_count)
            ],
            "total_time": time.perf_counter() - started_at,
        }

    async def run(self) -> dict[str, Any]:
        """Replay one selected trace window and publish its local and W&B results."""
        trace = self.benchmark.arrival_trace
        reader = ArrivalTraceReader(trace.trace_selector)
        trace_window_start, events = reader.read_window(
            start_offset_seconds=trace.start_offset_seconds,
            duration_seconds=trace.duration_seconds,
        )
        trace_format = reader.trace_format
        if trace_format is None:
            raise RuntimeError("Trace format was not detected")
        request_origin, events = bind_arrival_trace_requests(
            self.benchmark,
            self.endpoint,
            events,
        )
        request_count = len(events)

        max_concurrency = trace.max_concurrency
        active_connection_limit = (
            request_count
            if max_concurrency is None
            else min(max_concurrency, request_count)
        )
        reported_concurrency = (
            -1 if max_concurrency is None else max_concurrency
        )
        reporting_load = {
            "parallel": reported_concurrency,
            "number": request_count,
            "rate": -1.0,
            "open_loop": False,
            "resolved_parallel": reported_concurrency,
        }
        run_record = build_benchmark_run_record(
            self.benchmark,
            self.endpoint,
            "arrival_trace",
            reporting_load,
        )
        run_record.update(
            {
                "dataset": f"trace={trace.trace_selector}",
                "trace_path": trace.trace_selector,
                "payload_dataset": self.benchmark.resolved_dataset.dataset_selectors[0],
                "trace_start": trace.start_offset_seconds,
                "trace_duration": trace.duration_seconds,
                "trace_max_concurrency": max_concurrency,
                "trace_synthetic_prefix_reuse": trace.synthetic_prefix_reuse,
                "trace_format": trace_format,
                "payload_source": request_origin,
            }
        )
        publication = ResultPublication(
            self.benchmark,
            self.endpoint,
            run_record,
        )
        with publication:
            async with ChatCompletionsLoadClient(
                self.benchmark,
                self.endpoint,
                max_connections=active_connection_limit,
            ) as client:
                request_measurements = await self._replay_events(
                    client,
                    events,
                    max_concurrency=max_concurrency,
                    trace_window_start=trace_window_start,
                )
            metrics = summarize_http_measurements(
                self.benchmark,
                request_measurements,
                arrival_rate=-1.0,
                request_count=request_count,
                reported_concurrency=reported_concurrency,
                include_user_throughput=False,
            )
            self._attach_replay_metrics(
                metrics, request_measurements["results"]
            )
            publication.publish(
                request_measurements,
                metrics,
                trace_measurements=request_measurements["results"],
            )

        return {
            "mode": "arrival_trace",
            "metrics": metrics,
            "output_dir": publication.output_dir,
        }
