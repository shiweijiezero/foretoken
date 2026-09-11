# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind request tasks to recorded arrival events and replay them by timestamp."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.integrations.openai import ChatCompletionsLoadClient
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import (
    RequestMeasurement,
    percentile_summary,
    summarize_measurements,
)
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    write_json,
)
from benchmarks.datasets.conversations import (
    load_indexed_request_tasks,
    load_request_tasks,
)
from benchmarks.datasets.synthetic import (
    generate_synthetic_prefix_reuse_requests,
    generate_trace_random_requests,
)
from benchmarks.datasets.traces import ArrivalTraceEvent, ArrivalTraceReader

logger = logging.getLogger(__name__)


def _request_origin(benchmark: BenchmarkConfig) -> str:
    return (
        "random"
        if benchmark.resolved_workload.dataset_selectors == ["random"]
        else "dataset"
    )


def bind_arrival_trace_requests(
    benchmark: BenchmarkConfig,
    service: ModelService,
    events: list[ArrivalTraceEvent],
) -> tuple[str, list[ArrivalTraceEvent]]:
    """Bind native, random, or external-dataset request tasks to the selected arrival events."""
    request_origin = _request_origin(benchmark)
    workload = benchmark.resolved_workload
    trace = benchmark.trace
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
                service,
                input_lengths=[int(length) for length in input_lengths],
                hash_id_lists=[event.hash_ids for event in events],
            )
        else:
            requests = generate_trace_random_requests(
                benchmark,
                service,
                request_count=len(events),
                input_lengths=(
                    [int(length) for length in input_lengths]
                    if all(has_input_lengths)
                    else None
                ),
            )
    elif workload.dataset_selectors[0] == trace.trace_selector:
        if all(event.request is not None for event in events):
            return request_origin, [
                replace(event, request_origin=request_origin) for event in events
            ]
        requests = load_indexed_request_tasks(
            workload.dataset_selectors[0],
            [event.source_row_index for event in events],
        )
    else:
        requests = load_request_tasks(benchmark, request_count=len(events))

    if len(requests) != len(events):
        raise ValueError(
            f"Loaded {len(requests)} requests for {len(events)} trace events"
        )

    return request_origin, [
        replace(event, request=request, request_origin=request_origin)
        for event, request in zip(events, requests)
    ]


def _request_measurement(record: dict[str, Any]) -> RequestMeasurement:
    """Project one replay record onto the shared per-request measurement."""
    return RequestMeasurement(
        started_at=float(record["trace_offset_s"]) + float(record["replay_delay"]),
        ttft=record["ttft"],
        latency=float(record["latency"]),
        tpot=record["tpot"],
        itl_samples=(),
        input_tokens=int(record["input_tokens"]),
        output_tokens=int(record["output_tokens"]),
        succeeded=bool(record["success"]),
        conversation_id=record["conversation_id"],
        turn=None,
    )


class TraceReplayBenchmark:
    """Execute and summarize one timestamp-driven trace replay."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
    ) -> None:
        self.benchmark = benchmark
        self.service = service

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
        record = await client.send(event.request)
        record["source_index"] = event.source_row_index
        record["conversation_id"] = event.conversation_id
        record["trace_timestamp_s"] = event.timestamp_seconds
        record["trace_offset_s"] = trace_offset_seconds
        record["trace_input_length"] = event.input_tokens
        record["trace_hash_block_count"] = (
            len(event.hash_ids) if event.hash_ids is not None else None
        )
        record["payload_source"] = event.request_origin
        replay_delay = max(0.0, actual_send_at - scheduled_at)
        record["replay_delay"] = replay_delay
        record["trace_e2e_latency"] = replay_delay + float(record["latency"])
        if record["ttft"] is not None:
            record["trace_e2e_ttft"] = replay_delay + float(record["ttft"])
        else:
            record["trace_e2e_ttft"] = None
        return index, record

    @staticmethod
    def _attach_replay_metrics(
        metrics: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> None:
        successful = [item for item in records if item["success"]]
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
    ) -> tuple[list[dict[str, Any]], float]:
        """Schedule requests by absolute recorded offset and include concurrency waits in replay delay."""
        completed_tasks: asyncio.Queue[
            asyncio.Task[tuple[int, dict[str, Any]]]
        ] = asyncio.Queue()
        pending_tasks: set[
            asyncio.Task[tuple[int, dict[str, Any]]]
        ] = set()
        records_by_index: dict[int, dict[str, Any]] = {}

        def collect(task: asyncio.Task[tuple[int, dict[str, Any]]]) -> None:
            pending_tasks.discard(task)
            index, record = task.result()
            records_by_index[index] = record

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

        records = [records_by_index[index] for index in range(request_count)]
        return records, time.perf_counter() - started_at

    async def _replay(self) -> BenchmarkRun:
        """Read the trace window, bind requests, replay them, and publish the result."""
        trace = self.benchmark.trace
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
            self.service,
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
        record = build_benchmark_run_record(
            self.benchmark,
            self.service,
            "arrival_trace",
            reporting_load,
        )
        record.update(
            {
                "dataset": f"trace={trace.trace_selector}",
                "trace_path": trace.trace_selector,
                "payload_dataset": self.benchmark.resolved_workload.dataset_selectors[0],
                "trace_start": trace.start_offset_seconds,
                "trace_duration": trace.duration_seconds,
                "trace_max_concurrency": max_concurrency,
                "trace_synthetic_prefix_reuse": trace.synthetic_prefix_reuse,
                "trace_format": trace_format,
                "payload_source": request_origin,
            }
        )
        with ResultOutputs(self.benchmark, self.service, record) as outputs:
            async with ChatCompletionsLoadClient(
                self.benchmark,
                self.service,
                max_connections=active_connection_limit,
            ) as client:
                records, total_time = await self._replay_events(
                    client,
                    events,
                    max_concurrency=max_concurrency,
                    trace_window_start=trace_window_start,
                )
            measurements = [_request_measurement(item) for item in records]
            metrics = summarize_measurements(
                measurements,
                total_time=total_time,
                stream=self.benchmark.generation.stream,
                arrival_rate=-1.0,
                request_count=request_count,
                reported_concurrency=reported_concurrency,
                include_user_throughput=False,
            )
            self._attach_replay_metrics(metrics, records)
            # The raw replay records carry trace timing that RequestMeasurement
            # does not; they are written as an artifact for the W&B trace charts.
            raw_output: Path = write_json(
                outputs.execution_dir, "raw_output.json", records
            )
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts={"raw_output": raw_output},
            )
            outputs.publish(run)
        return run

    def run(self) -> BenchmarkRun:
        """Replay one selected trace window and return its published result."""
        return asyncio.run(self._replay())
