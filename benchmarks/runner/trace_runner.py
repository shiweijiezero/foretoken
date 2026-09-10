# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Replay timestamped conversation traces against one OpenAI-compatible URL."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from benchmarks.client.openai_client import OpenAICompatClient
from benchmarks.metrics.aggregator import percentile_stats
from benchmarks.runner.base import Runner
from benchmarks.workload.trace_loader import TraceRequest
from benchmarks.workload.trace_workload import load_trace_workload


class TraceRunner(Runner):
    """Replay recorded user turns while retaining dataset-provided context."""

    async def _send(
        self,
        client: OpenAICompatClient,
        request: TraceRequest,
        index: int,
        *,
        scheduled_at: float,
        trace_offset_s: float,
    ) -> tuple[int, dict[str, Any]]:
        actual_send_at = time.perf_counter()
        result = await self.generate_request(
            client,
            prompt=request.prompt,
            messages=request.messages,
            tools=request.tools,
        )
        result["source_index"] = request.source_index
        result["conversation_id"] = request.conversation_id
        result["trace_timestamp_s"] = request.timestamp_s
        result["trace_offset_s"] = trace_offset_s
        result["trace_input_length"] = request.input_length
        result["trace_hash_block_count"] = (
            len(request.hash_ids) if request.hash_ids is not None else None
        )
        result["payload_source"] = request.payload_source
        replay_delay = max(0.0, actual_send_at - scheduled_at)
        result["replay_delay"] = replay_delay
        result["trace_e2e_latency"] = replay_delay + float(result["latency"])
        if result["ttft"] is not None:
            result["trace_e2e_ttft"] = replay_delay + float(result["ttft"])
        else:
            result["trace_e2e_ttft"] = None
        return index, result

    @staticmethod
    def _add_trace_metrics(
        metrics: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> None:
        successful = [result for result in results if result["success"]]
        for name in ("replay_delay", "trace_e2e_ttft", "trace_e2e_latency"):
            values = [
                float(result[name])
                for result in successful
                if result[name] is not None
            ]
            metrics[name] = percentile_stats(values)

    async def _replay(
        self,
        client: OpenAICompatClient,
        requests: list[TraceRequest],
        *,
        max_concurrency: int | None,
        trace_window_start_s: float,
    ) -> dict[str, Any]:
        """Schedule trace requests at their recorded arrival times."""

        task_queue: asyncio.Queue[
            asyncio.Task[tuple[int, dict[str, Any]]]
        ] = asyncio.Queue()
        pending: set[asyncio.Task[tuple[int, dict[str, Any]]]] = set()
        completed: dict[int, dict[str, Any]] = {}

        def collect(task: asyncio.Task[tuple[int, dict[str, Any]]]) -> None:
            pending.discard(task)
            index, result = task.result()
            completed[index] = result

        def drain_completed() -> None:
            while True:
                try:
                    collect(task_queue.get_nowait())
                except asyncio.QueueEmpty:
                    return

        start_time = time.perf_counter()
        request_count = 0
        try:
            for request in requests:
                scheduled_at = (
                    start_time + request.timestamp_s - trace_window_start_s
                )
                delay = scheduled_at - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

                drain_completed()
                while (
                    max_concurrency is not None
                    and len(pending) >= max_concurrency
                ):
                    collect(await task_queue.get())

                task = asyncio.create_task(
                    self._send(
                        client,
                        request,
                        request_count,
                        scheduled_at=scheduled_at,
                        trace_offset_s=(
                            request.timestamp_s - trace_window_start_s
                        ),
                    )
                )
                pending.add(task)
                task.add_done_callback(task_queue.put_nowait)
                request_count += 1

            while pending:
                collect(await task_queue.get())
        finally:
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        end_time = time.perf_counter()

        return {
            "results": [completed[index] for index in range(request_count)],
            "total_time": end_time - start_time,
        }

    async def run(self) -> dict[str, Any]:
        dataset = self.config.dataset
        (
            trace_window_start_s,
            trace_format,
            payload_source,
            requests,
        ) = load_trace_workload(self.config)
        request_count = len(requests)
        if request_count < 1:
            raise ValueError("selected trace window contains no requests")

        max_concurrency = dataset.trace_max_concurrency
        active_limit = (
            request_count
            if max_concurrency is None
            else min(max_concurrency, request_count)
        )
        reported_parallel = -1 if max_concurrency is None else max_concurrency
        reporting_load = {
            "parallel": reported_parallel,
            "number": request_count,
            "rate": -1.0,
            "open_loop": False,
            "resolved_parallel": reported_parallel,
        }
        writer = self.create_writer()
        run_config = self.build_run_config("trace", reporting_load)
        run_config.update(
            {
                "dataset": f"trace={dataset.trace_path}",
                "trace_path": dataset.trace_path,
                "payload_dataset": dataset.dataset[0],
                "trace_start": dataset.trace_start,
                "trace_duration": dataset.trace_duration,
                "trace_max_concurrency": max_concurrency,
                "trace_synthetic_prefix_reuse": (
                    dataset.trace_synthetic_prefix_reuse
                ),
                "trace_format": trace_format,
                "payload_source": payload_source,
            }
        )
        wandb_logger = self.create_wandb_logger(writer, reporting_load)
        client = self.create_client(active_limit, request_count)

        try:
            raw_output = await self._replay(
                client,
                requests,
                max_concurrency=max_concurrency,
                trace_window_start_s=trace_window_start_s,
            )
            metrics = self.aggregate_metrics(
                raw_output,
                rate=-1.0,
                number=request_count,
                resolved_parallel=reported_parallel,
                include_user_throughput=False,
            )
            self._add_trace_metrics(metrics, raw_output["results"])
            self.save_results(
                writer,
                run_config,
                raw_output,
                metrics,
                wandb_logger=wandb_logger,
                wandb_trace_results=raw_output["results"],
            )
        except Exception:
            wandb_logger.finish()
            raise
        finally:
            await client.close()

        return {
            "mode": "trace",
            "metrics": metrics,
            "output_dir": writer.output_dir,
        }
