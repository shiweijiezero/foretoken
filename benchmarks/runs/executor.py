# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execute task-based HTTP workloads with one shared arrival and admission loop."""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import (
    Task,
    load_conversation_tasks,
    load_request_tasks,
    split_chat_conversation,
)
from benchmarks.datasets.synthetic import (
    generate_trace_random_requests,
    iter_duration_random_requests,
)
from benchmarks.integrations.openai import ChatCompletionsLoadClient
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import RequestMeasurement, summarize_measurement_groups, summarize_measurements
from benchmarks.results.output import (
    BenchmarkRun,
    ResultOutputs,
    build_benchmark_run_record,
    resolved_load_record,
)

if TYPE_CHECKING:
    from benchmarks.profiling.capture import BenchmarkProfile


class TaskLoadBenchmark:
    """Run one or more task sources with global scheduling, admission, and result ownership."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        tasks: Iterable[Task] | None = None,
        dataset_tasks: dict[str, list[Task]] | None = None,
        label: str = "",
        output_dir: str | None = None,
        wandb_group: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.tasks = list(tasks) if tasks is not None else None
        self.dataset_tasks = dataset_tasks
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self._conversation_attempted = 0
        self._conversation_completed = 0

    def _load_tasks(self) -> Iterable[Task]:
        if self.tasks is not None:
            return self.tasks
        workload = self.benchmark.resolved_workload
        if workload.dataset_selectors == ["random"]:
            count = self.benchmark.load.request_count
            if count is None:
                return iter_duration_random_requests(self.benchmark, self.service)
            return generate_trace_random_requests(self.benchmark, self.service, request_count=count)
        if workload.fixed_prompt and not workload.dataset_selectors:
            count = self.benchmark.load.request_count
            if count is None:
                return itertools.repeat(load_request_tasks(self.benchmark, request_count=1)[0])
            return load_request_tasks(self.benchmark, request_count=count)
        if self.dataset_tasks is not None:
            return [task for tasks in self.dataset_tasks.values() for task in tasks]
        if self.benchmark.is_multi_turn:
            return load_conversation_tasks(self.benchmark)
        count = self.benchmark.load.request_count
        if count is None:
            return load_request_tasks(self.benchmark, request_count=None)
        return load_request_tasks(self.benchmark, request_count=count)

    def _task_stream(self, tasks: Iterable[Task]):
        budget = self.benchmark.load.request_count
        stream = iter(tasks)
        if budget is None:
            if self.dataset_tasks is not None:
                sources = [source for source, rows in self.dataset_tasks.items() if rows]
                weights = self.benchmark.resolved_workload.dataset_weights
                selected = self.benchmark.resolved_workload.dataset_selectors
                relative = [weights[selected.index(source)] / max(weights) if weights else 1.0 for source in sources]
                rng = random.Random(self.benchmark.resolved_workload.random_seed)
                cycles = {source: itertools.cycle(self.dataset_tasks[source]) for source in sources}
                return (next(cycles[rng.choices(sources, weights=relative)[0]]) for _ in itertools.count())
            return stream if hasattr(tasks, "__next__") else itertools.cycle(tasks)
        return itertools.islice(stream, budget)

    def _next_interval(self, generator: random.Random) -> float:
        load = self.benchmark.load
        if load.arrival_pattern == "constant":
            return 1.0 / load.arrival_rate
        if load.arrival_pattern == "poisson":
            return generator.expovariate(load.arrival_rate)
        shape = load.burstiness
        return generator.gammavariate(shape, 1.0 / (load.arrival_rate * shape))

    async def _run_requests(
        self,
        *,
        warmup: bool = False,
        profile: BenchmarkProfile | None = None,
    ) -> tuple[list[RequestMeasurement], float]:
        tasks = self._load_tasks()
        if not tasks:
            return [], 0.0
        load = self.benchmark.load
        deadline = load.duration_seconds
        budget = load.request_count
        if warmup and budget is None:
            budget = load.warmup_requests
        if budget is not None:
            budget = int(budget)
        stream = self._task_stream(tasks)
        if budget is not None:
            stream = iter(itertools.islice(stream, budget))
        semaphore = asyncio.Semaphore(load.max_concurrency) if load.max_concurrency > 0 else None
        rate = load.arrival_rate
        rng = random.Random(self.benchmark.resolved_workload.random_seed + (1 if warmup else 0))
        measurements: list[RequestMeasurement] = []
        lock = asyncio.Lock()
        attempted_conversations = 0
        completed_conversations = 0
        budget_lock = asyncio.Lock()
        remaining_requests = budget
        active: set[asyncio.Task[None]] = set()

        async with ChatCompletionsLoadClient(
            self.benchmark,
            self.service,
            max_connections=load.max_concurrency if load.max_concurrency > 0 else None,
        ) as client, asyncio.TaskGroup() as request_tasks:
            if profile is not None:
                await profile.before_request()
            started = time.perf_counter()

            async def run_task(task: Task, scheduled_at: float) -> None:
                nonlocal remaining_requests, attempted_conversations, completed_conversations
                if deadline is not None:
                    remaining = deadline - (time.perf_counter() - started)
                    if remaining <= 0:
                        return
                acquired = False
                try:
                    if semaphore is not None:
                        if deadline is None:
                            await semaphore.acquire()
                            acquired = True
                        else:
                            remaining = deadline - (time.perf_counter() - started)
                            if remaining <= 0:
                                return
                            try:
                                await asyncio.wait_for(semaphore.acquire(), remaining)
                            except asyncio.TimeoutError:
                                return
                            acquired = True
                    context: list[dict[str, Any]] = []
                    turns = [(task.messages(), None)]
                    generated_history = self.benchmark.resolved_workload.conversation_history == "generated"
                    if self.benchmark.is_multi_turn:
                        turns = split_chat_conversation(task.messages())
                        max_turns = self.benchmark.resolved_workload.max_turns
                        if max_turns is not None and max_turns > 0:
                            turns = turns[:max_turns]
                        async with lock:
                            attempted_conversations += 1
                    sent_turns = 0
                    conversation_succeeded = True
                    for turn_index, (turn, reference_answer) in enumerate(turns):
                        if deadline is not None and time.perf_counter() - started >= deadline:
                            break
                        async with budget_lock:
                            if remaining_requests is not None:
                                if remaining_requests <= 0:
                                    break
                                remaining_requests -= 1
                        if profile is not None:
                            await profile.before_request()
                        response = await client.send_messages(
                            context + turn,
                            task.metadata,
                        )
                        if generated_history and turn_index < len(turns) - 1 and response.get("tool_calls"):
                            response["success"] = False
                            response["error"] = "Model requested tool execution before the next turn; a harness is required"
                        if profile is not None:
                            profile.response_received(bool(response["success"]))
                        item = RequestMeasurement(
                            started_at=float(response["started_at"]) - started,
                            ttft=response["ttft"],
                            latency=float(response["latency"]),
                            tpot=response["tpot"],
                            itl_samples=tuple(response["inter_token_latencies"]),
                            input_tokens=response["input_tokens"],
                            output_tokens=response["output_tokens"],
                            cached_input_tokens=response["cached_input_tokens"],
                            succeeded=bool(response["success"]),
                            conversation_id=task.id,
                            turn=turn_index if self.benchmark.is_multi_turn else None,
                            status_code=response["status_code"],
                            error_message=response["error"],
                            dataset=task.metadata.get("_dataset") or (self.benchmark.resolved_workload.dataset_selectors[0] if self.benchmark.resolved_workload.dataset_selectors else None),
                            model=response["model"],
                            priority=task.metadata.get("priority"),
                            request_class=task.metadata.get("request_class"),
                            target_output_tokens=response["target_output_tokens"],
                        )
                        async with lock:
                            measurements.append(item)
                        sent_turns += 1
                        if not response["success"]:
                            conversation_succeeded = False
                            break
                        context.extend(turn)
                        if generated_history:
                            context.append({"role": "assistant", "content": response["generated_text"]})
                        elif reference_answer is not None:
                            context.append(reference_answer)
                    if self.benchmark.is_multi_turn and conversation_succeeded and sent_turns == len(turns):
                        async with lock:
                            completed_conversations += 1
                finally:
                    if acquired:
                        semaphore.release()

            next_at = 0.0
            first_arrival = True
            while True:
                if deadline is not None and time.perf_counter() - started >= deadline:
                    break
                try:
                    task = next(stream)
                except StopIteration:
                    break
                if rate != -1:
                    if not first_arrival:
                        next_at += self._next_interval(rng)
                    first_arrival = False
                    delay = started + next_at - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if deadline is not None and time.perf_counter() - started >= deadline:
                        break
                elif deadline is not None and time.perf_counter() - started >= deadline:
                    break
                pending = request_tasks.create_task(run_task(task, next_at))
                active.add(pending)
                pending.add_done_callback(active.discard)
                if load.max_concurrency > 0 and len(active) >= load.max_concurrency:
                    await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
        self._conversation_attempted = attempted_conversations
        self._conversation_completed = completed_conversations
        return measurements, time.perf_counter() - started

    def _warmup_config(self) -> BenchmarkConfig:
        return replace(
            self.benchmark,
            load=replace(self.benchmark.load, request_count=self.benchmark.load.warmup_requests, warmup_requests=0, duration_seconds=None),
        )

    def run(self) -> BenchmarkRun:
        """Run warmup before opening measured observers, then publish one measured result."""
        with ResultOutputs(
            self.benchmark, self.service, label=self.label,
            output_dir=self.output_dir, wandb_group=self.wandb_group,
        ) as outputs:
            if self.benchmark.resolved_workload.has_multiple_datasets and self.dataset_tasks is None:
                grouped = load_multi_dataset_tasks(self.benchmark)
                self.tasks = grouped.pop("__global__")
                self.dataset_tasks = grouped
            if self.benchmark.load.warmup_requests:
                warmup = TaskLoadBenchmark(
                    self._warmup_config(), self.service, tasks=self.tasks,
                    dataset_tasks=self.dataset_tasks,
                )
                warmup_measurements, _ = asyncio.run(warmup._run_requests(warmup=True))
                if not warmup_measurements or any(not item.succeeded for item in warmup_measurements):
                    raise ValueError("Warmup requests failed; measurement was not started")
            record = build_benchmark_run_record(
                self.benchmark, self.service, "task_load", resolved_load_record(self.benchmark)
            )
            record["warmup_requests"] = self.benchmark.load.warmup_requests
            if self.dataset_tasks is not None:
                record["datasets"] = list(self.dataset_tasks)
            outputs.open(record)
            profile = outputs.create_profile()
            with (profile if profile is not None else nullcontext()):
                measurements, elapsed = asyncio.run(self._run_requests(profile=profile))
            if profile is not None:
                artifacts = {"profile": Path(outputs.execution_dir) / "profile.json"}
            else:
                artifacts = {}
            metrics = summarize_measurements(
                measurements,
                total_time=elapsed,
                stream=self.benchmark.generation.stream,
                arrival_rate=self.benchmark.load.arrival_rate,
                request_count=len(measurements) if self.benchmark.load.request_count is None else self.benchmark.load.request_count,
                reported_concurrency=self.benchmark.load.max_concurrency,
                gpu_count=self.service.gpu_count if {item.model for item in measurements} == {self.service.model} else None,
                slo_criteria=(self.benchmark.slo.params[0] if self.benchmark.slo.params else None),
                slo_by_class=self.benchmark.slo.by_class,
            )
            metrics.update(summarize_measurement_groups(
                measurements, total_time=elapsed, stream=self.benchmark.generation.stream,
                arrival_rate=self.benchmark.load.arrival_rate,
                reported_concurrency=self.benchmark.load.max_concurrency,
                slo_criteria=(self.benchmark.slo.params[0] if self.benchmark.slo.params else None),
                slo_by_class=self.benchmark.slo.by_class,
                include_single_dataset=self.dataset_tasks is not None,
            ))
            if self.benchmark.is_multi_turn:
                metrics["multi_turn"] = True
                metrics["conversation"] = {
                    "attempted_num": self._conversation_attempted,
                    "completed_num": self._conversation_completed,
                    "request_num": len(measurements),
                    "max_turns": self.benchmark.resolved_workload.max_turns,
                    "avg_turn_requests": (
                        len(measurements) / self._conversation_attempted
                        if self._conversation_attempted else 0.0
                    ),
                    "attempted_conversations_per_second": (
                        self._conversation_attempted / elapsed if elapsed > 0 else 0.0
                    ),
                }
            run = BenchmarkRun(
                record=record,
                metrics=metrics,
                measurements=measurements,
                artifacts=artifacts,
                time_origin=time.perf_counter() - elapsed,
            )
            outputs.publish(run)
        return run


def load_multi_dataset_tasks(benchmark: BenchmarkConfig) -> dict[str, list[Task]]:
    """Load all selected datasets and interleave them into one global workload source."""
    workload = benchmark.resolved_workload
    result: dict[str, list[Task]] = {}
    total = benchmark.load.request_count
    weights = workload.dataset_weights or [1.0] * len(workload.dataset_selectors)
    if total is not None:
        largest = max(weights)
        scaled = [weight / largest for weight in weights]
        shares = [total * weight / sum(scaled) for weight in scaled]
        counts = [int(share) for share in shares]
        for index in sorted(range(len(counts)), key=lambda i: (-(shares[i] - counts[i]), i))[:total - sum(counts)]:
            counts[index] += 1
    else:
        counts = [None] * len(weights)
    for index, selector in enumerate(workload.dataset_selectors):
        child = replace(workload, dataset_selectors=[selector], fixed_prompt="")
        child_benchmark = replace(benchmark, workload=child)
        count = counts[index]
        if count == 0:
            result[selector] = []
            continue
        child_benchmark = replace(child_benchmark, load=replace(benchmark.load, request_count=count))
        tasks = load_conversation_tasks(child_benchmark) if benchmark.is_multi_turn else load_request_tasks(child_benchmark, request_count=count)
        result[selector] = [replace(task, metadata={**task.metadata, "_dataset": selector}) for task in tasks]
    # Spread each source across the complete run while preserving its row order.
    # Equal shares remain round-robin; unequal shares do not leave a single-source tail.
    scheduled = []
    for tasks in result.values():
        scheduled.extend(((index + 0.5) / len(tasks), task) for index, task in enumerate(tasks))
    scheduled.sort(key=lambda item: item[0])
    return {"__global__": [task for _, task in scheduled], **result}
