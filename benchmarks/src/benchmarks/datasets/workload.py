# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare reusable request sources and independent phase-local conversation streams."""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import (
    Task,
    load_conversation_tasks,
    load_request_tasks,
    split_chat_conversation,
)
from benchmarks.datasets.huggingface import resolve_tokenizer_path
from benchmarks.datasets.replay import bind_arrival_trace_requests
from benchmarks.datasets.synthetic import RandomRequestSource
from benchmarks.datasets.traces import ArrivalTraceEvent, ArrivalTraceReader
from benchmarks.integrations.openai import PreparedRequest, RequestBuilder
from benchmarks.model_service import ModelService


@dataclass(frozen=True)
class RequestTurn:
    """One prepared request delta and its optional recorded assistant answer."""

    request: PreparedRequest
    answer: dict[str, Any] | None


@dataclass(frozen=True)
class RequestEpisode:
    """A scheduled independent request or causal conversation with source identity."""

    task: Task
    turns: tuple[RequestTurn, ...]
    conversation: bool
    trace: ArrivalTraceEvent | None = None


class HttpWorkload:
    """Own input preparation and restartable streams; each phase gets its own sampling state."""

    def __init__(self, benchmark: BenchmarkConfig, service: ModelService) -> None:
        self.benchmark = benchmark
        self.service = service
        self.builder = RequestBuilder(benchmark, service)
        self.dataset = benchmark.resolved_workload
        self.trace_events: list[ArrivalTraceEvent] | None = None
        self.trace_start = 0.0
        self.trace_format: str | None = None
        self.request_origin: str | None = None
        self.random_source: RandomRequestSource | None = None
        self.dataset_tasks: dict[str, list[Task]] | None = None
        self.scripts: dict[
            str, list[tuple[list[dict[str, Any]], dict[str, Any] | None]]
        ] = {}
        self.reference_lengths: dict[tuple[str, int], int] = {}

        if benchmark.trace.trace_selector:
            reader = ArrivalTraceReader(benchmark.trace.trace_selector)
            self.trace_start, events = reader.read_window(
                start_offset_seconds=benchmark.trace.start_offset_seconds,
                duration_seconds=benchmark.trace.duration_seconds,
            )
            if benchmark.slo.search and benchmark.load.request_count is not None:
                events = events[: benchmark.load.request_count]
            self.trace_format = reader.trace_format
            self.request_origin, self.trace_events = bind_arrival_trace_requests(
                benchmark, service, events
            )
            self.tasks = [event.request for event in self.trace_events]
        elif self.dataset.dataset_selectors == ["random"]:
            self.random_source = RandomRequestSource(benchmark, service)
            self.tasks = (
                list(
                    itertools.islice(
                        self.random_source.requests(), benchmark.load.request_count
                    )
                )
                if benchmark.load.request_count is not None
                else []
            )
        elif self.dataset.has_multiple_datasets:
            grouped = load_multi_dataset_tasks(benchmark)
            self.tasks = grouped.pop("__global__")
            self.dataset_tasks = grouped
        else:
            self.tasks = (
                load_conversation_tasks(benchmark)
                if benchmark.is_multi_turn
                else load_request_tasks(benchmark)
            )
        if not self.tasks and self.random_source is None:
            raise ValueError("The selected workload contains no requests")
        self.request_count = (
            len(self.tasks)
            if self.trace_events is not None
            else benchmark.load.request_count
        )
        self._prepare_conversations()

    def _prepare_conversations(self) -> None:
        """Resolve scripts and reference output lengths once, outside all measurement clocks."""
        if not self.benchmark.is_multi_turn:
            return
        tokenizers: dict[tuple[str, str], Any] = {}
        for task in self.tasks:
            if task.prompt_token_ids is not None:
                continue
            turns = split_chat_conversation(task.messages())
            if self.dataset.max_turns is not None and self.dataset.max_turns > 0:
                turns = turns[: self.dataset.max_turns]
            self.scripts[task.id] = turns
            if (
                "output_length" in task.metadata
                or self.benchmark.generation.min_output_length is not None
            ):
                continue
            for index, (_, answer) in enumerate(turns):
                if answer is None or answer.get("tool_calls"):
                    continue
                text = answer.get("content")
                if isinstance(text, list) and all(
                    part.get("type") == "text" for part in text
                ):
                    text = "".join(part["text"] for part in text)
                if not isinstance(text, str) or not text:
                    continue
                identity = (
                    ("hf", self.dataset.tokenizer)
                    if self.dataset.tokenizer
                    else replace(
                        self.service, model=self.builder.model_for(task.metadata)
                    ).tokenizer_identity
                )
                if identity not in tokenizers:
                    from transformers import AutoTokenizer

                    source, name = identity
                    tokenizers[identity] = AutoTokenizer.from_pretrained(
                        resolve_tokenizer_path(name, source=source)
                    )
                length = len(
                    tokenizers[identity].encode(text, add_special_tokens=False)
                )
                if length:
                    self.reference_lengths[task.id, index] = length

    def _tasks(self, *, warmup: bool) -> Iterator[Task]:
        """Restart a finite source or produce its duration-bounded continuation."""
        if self.random_source is not None and not self.tasks:
            return self.random_source.requests()
        if not warmup and self.request_count is None and self.dataset_tasks is not None:
            sources = [source for source, tasks in self.dataset_tasks.items() if tasks]
            weights = self.dataset.dataset_weights
            selected = self.dataset.dataset_selectors
            relative = [
                weights[selected.index(source)] if weights else 1.0
                for source in sources
            ]
            rng = random.Random(self.dataset.random_seed)
            cycles = {
                source: itertools.cycle(self.dataset_tasks[source])
                for source in sources
            }
            return (
                next(cycles[rng.choices(sources, weights=relative)[0]])
                for _ in itertools.count()
            )
        if warmup or self.request_count is None:
            return itertools.cycle(self.tasks)
        return iter(self.tasks)

    def episodes(self, *, warmup: bool = False) -> Iterator[RequestEpisode]:
        """Prepare request occurrences without consuming another phase's inputs or RNG."""
        remaining = (
            self.benchmark.load.warmup_requests if warmup else self.request_count
        )
        for index, task in enumerate(self._tasks(warmup=warmup)):
            if remaining is not None and remaining <= 0:
                break
            # Resolve each episode's lengths before it enters concurrent execution.
            rng = random.Random(self.dataset.random_seed + index)
            conversation = (
                self.benchmark.is_multi_turn and task.prompt_token_ids is None
            )
            script = (
                self.scripts[task.id] if conversation else [(task.messages(), None)]
            )
            if remaining is not None:
                script = script[:remaining]
                remaining -= len(script)
            turns = tuple(
                RequestTurn(
                    self.builder.prepare(
                        task,
                        messages,
                        rng,
                        reference_length=self.reference_lengths.get((task.id, turn)),
                    ),
                    answer,
                )
                for turn, (messages, answer) in enumerate(script)
            )
            event = (
                self.trace_events[index % len(self.trace_events)]
                if self.trace_events is not None
                else None
            )
            yield RequestEpisode(task, turns, conversation, event)


def load_multi_dataset_tasks(benchmark: BenchmarkConfig) -> dict[str, list[Task]]:
    """Allocate the HTTP budget across sources and interleave their ordered conversation scripts."""
    workload = benchmark.resolved_workload
    result: dict[str, list[Task]] = {}
    total = benchmark.load.request_count
    weights = workload.dataset_weights or [1.0] * len(workload.dataset_selectors)
    if total is not None:
        largest = max(weights)
        scaled = [weight / largest for weight in weights]
        shares = [total * weight / sum(scaled) for weight in scaled]
        counts = [int(share) for share in shares]
        for index in sorted(
            range(len(counts)), key=lambda i: (-(shares[i] - counts[i]), i)
        )[: total - sum(counts)]:
            counts[index] += 1
    else:
        counts = [None] * len(weights)
    for index, selector in enumerate(workload.dataset_selectors):
        child = replace(workload, dataset_selectors=[selector], fixed_prompt="")
        child_benchmark = replace(
            benchmark,
            workload=child,
            load=replace(benchmark.load, request_count=counts[index]),
        )
        if counts[index] == 0:
            result[selector] = []
            continue
        tasks = (
            load_conversation_tasks(child_benchmark)
            if benchmark.is_multi_turn
            else load_request_tasks(child_benchmark)
        )
        result[selector] = [
            replace(task, metadata={**task.metadata, "_dataset": selector})
            for task in tasks
        ]
    scheduled = [
        ((index + 0.5) / len(tasks), task)
        for tasks in result.values()
        for index, task in enumerate(tasks)
    ]
    scheduled.sort(key=lambda item: item[0])
    return {"__global__": [task for _, task in scheduled], **result}
