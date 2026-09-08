# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Replay independent Chat Completions requests by recorded time and measure scheduling delay."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from benchmarks.performance.arrival_trace_records import ArrivalTraceEvent
from benchmarks.performance.arrival_trace_requests import (
    load_arrival_trace_requests,
)
from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.chat_client import ChatCompletionsLoadClient
from benchmarks.performance.http_benchmark import (
    build_benchmark_run_record,
    open_local_result_directory,
    publish_benchmark_results,
    summarize_http_measurements,
)
from benchmarks.performance.request_metrics import percentile_summary
from benchmarks.performance.wandb_results import WandbBenchmarkRun


class ArrivalTraceBenchmark:
    """Own the lifecycle of recorded-time scheduling, concurrency limits, and trace results."""

    def __init__(self, benchmark: HttpBenchmarkConfig) -> None:
        self.benchmark = benchmark

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
        """Replay the selected trace window and publish the compatible trace result structure."""
        trace = self.benchmark.arrival_trace
        (
            trace_window_start,
            trace_format,
            request_origin,
            events,
        ) = load_arrival_trace_requests(self.benchmark)
        request_count = len(events)
        if request_count < 1:
            raise ValueError("selected trace window contains no requests")

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
        result_directory = open_local_result_directory(self.benchmark)
        run_record = build_benchmark_run_record(
            self.benchmark,
            "arrival_trace",
            reporting_load,
        )
        run_record.update(
            {
                "dataset": f"trace={trace.trace_selector}",
                "trace_path": trace.trace_selector,
                "payload_dataset": self.benchmark.request_dataset.dataset_selectors[0],
                "trace_start": trace.start_offset_seconds,
                "trace_duration": trace.duration_seconds,
                "trace_max_concurrency": max_concurrency,
                "trace_synthetic_prefix_reuse": trace.synthetic_prefix_reuse,
                "trace_format": trace_format,
                "payload_source": request_origin,
            }
        )
        wandb_run = WandbBenchmarkRun()
        wandb_run.start(
            self.benchmark,
            output_dir=result_directory.output_dir,
            parallel=reported_concurrency,
            rate=-1.0,
        )

        try:
            async with ChatCompletionsLoadClient(
                self.benchmark,
                max_concurrency=active_connection_limit,
                request_count=request_count,
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
            publish_benchmark_results(
                self.benchmark,
                result_directory,
                run_record,
                request_measurements,
                metrics,
                wandb_run=wandb_run,
                trace_measurements=request_measurements["results"],
            )
        except Exception:
            wandb_run.finish()
            raise

        return {
            "mode": "arrival_trace",
            "metrics": metrics,
            "output_dir": result_directory.output_dir,
        }
