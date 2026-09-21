# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run conversation workloads with an HTTP request budget owned by Foretoken."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import (
    load_conversation_tasks,
    split_chat_conversation,
)
from benchmarks.integrations.openai import ChatCompletionsLoadClient
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import RequestMeasurement, summarize_measurements
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    resolved_load_record,
)


class ConversationBudgetBenchmark:
    """Execute multi-turn tasks until the HTTP request budget is exhausted."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        label: str = "",
        output_dir: str | None = None,
        wandb_group: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group

    async def _run_requests(self) -> tuple[list[RequestMeasurement], int, int, float]:
        tasks = load_conversation_tasks(self.benchmark)
        budget = self.benchmark.load.request_count
        worker_count = len(tasks)
        if self.benchmark.load.max_concurrency > 0:
            worker_count = min(worker_count, self.benchmark.load.max_concurrency)
        if not worker_count:
            return [], 0, 0, 0.0

        queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        for index, task in enumerate(tasks):
            await queue.put((index, task))
        budget_lock = asyncio.Lock()
        results_lock = asyncio.Lock()
        remaining = budget
        measurements: list[RequestMeasurement] = []
        attempted_conversations = 0
        completed_conversations = 0
        run_started_at = time.perf_counter()

        async with ChatCompletionsLoadClient(
            self.benchmark,
            self.service,
            max_connections=max(1, worker_count),
        ) as client:
            async def worker() -> None:
                nonlocal remaining, attempted_conversations, completed_conversations
                while True:
                    try:
                        conversation_id, task = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    turns = split_chat_conversation(task.messages())
                    max_turns = self.benchmark.resolved_workload.max_turns
                    if max_turns is not None and max_turns > 0:
                        turns = turns[:max_turns]
                    context: list[dict[str, Any]] = []
                    sent_turns = 0
                    for turn_index, turn in enumerate(turns):
                        async with budget_lock:
                            if remaining <= 0:
                                break
                            remaining -= 1
                            attempted_conversations += 1 if sent_turns == 0 else 0
                        response = await client.send_messages(
                            context + turn,
                            task.metadata,
                        )
                        measurement = RequestMeasurement(
                            started_at=float(response["started_at"]) - run_started_at,
                            ttft=response["ttft"],
                            latency=float(response["latency"]),
                            tpot=response["tpot"],
                            itl_samples=tuple(response["inter_token_latencies"]),
                            input_tokens=response["input_tokens"],
                            output_tokens=response["output_tokens"],
                            cached_input_tokens=response["cached_input_tokens"],
                            succeeded=bool(response["success"]),
                            conversation_id=task.id,
                            turn=turn_index,
                            status_code=response["status_code"],
                            error_message=response["error"],
                        )
                        async with results_lock:
                            measurements.append(measurement)
                        sent_turns += 1
                        if not response["success"]:
                            break
                        context.extend(turn)
                        context.append(
                            {
                                "role": "assistant",
                                "content": response["generated_text"],
                            }
                        )
                    if sent_turns == len(turns) and sent_turns > 0:
                        async with results_lock:
                            completed_conversations += 1
                    queue.task_done()

            await asyncio.gather(*(worker() for _ in range(worker_count)))
        duration = time.perf_counter() - run_started_at
        return measurements, attempted_conversations, completed_conversations, duration

    def run(self) -> BenchmarkRun:
        """Run one request-budgeted conversation workload and publish measurements."""
        load_record = resolved_load_record(self.benchmark)
        record = build_benchmark_run_record(
            self.benchmark,
            self.service,
            "conversation_load",
            load_record,
        )
        with ResultOutputs(
            self.benchmark,
            self.service,
            record,
            label=self.label,
            output_dir=self.output_dir,
            wandb_group=self.wandb_group,
        ) as outputs:
            measurements, attempted, completed, duration = asyncio.run(
                self._run_requests()
            )
            metrics = summarize_measurements(
                measurements,
                total_time=duration,
                stream=self.benchmark.generation.stream,
                arrival_rate=self.benchmark.load.arrival_rate,
                request_count=self.benchmark.load.request_count,
                reported_concurrency=self.benchmark.load.max_concurrency,
                gpu_count=self.service.gpu_count,
                slo_criteria=(self.benchmark.slo.params[0] if self.benchmark.slo.params else None),
            )
            metrics["multi_turn"] = True
            metrics["conversation"] = {
                "attempted_num": attempted,
                "completed_num": completed,
                "request_num": metrics["request_num"],
                "max_turns": self.benchmark.resolved_workload.max_turns,
                "avg_turn_requests": (
                    metrics["request_num"] / attempted if attempted else 0.0
                ),
                "attempted_conversations_per_second": (
                    attempted / duration if duration > 0 else 0.0
                ),
                "avg_context_turns_per_request": None,
                "latency": {"mean": None, "p50": None, "p95": None, "p99": None},
                "first_turn_ttft": {
                    "mean": None, "p50": None, "p95": None, "p99": None
                },
                "time_to_final_answer_token": {
                    "mean": None, "p50": None, "p95": None, "p99": None
                },
                "decode_tokens_per_second": {
                    "mean": None, "p50": None, "p95": None, "p99": None
                },
            }
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts={},
            )
            outputs.publish(run)
        return run
