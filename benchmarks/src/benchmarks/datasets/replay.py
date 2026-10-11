# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind trace timestamps and row metadata to independent HTTP request inputs."""

from __future__ import annotations

from dataclasses import replace

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import (
    load_indexed_request_tasks,
    load_request_tasks,
)
from benchmarks.datasets.huggingface import same_dataset_source
from benchmarks.datasets.synthetic import (
    generate_synthetic_prefix_reuse_requests,
    generate_trace_random_requests,
)
from benchmarks.datasets.traces import ArrivalTraceEvent
from benchmarks.model_service import ModelService


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
    elif same_dataset_source(workload.dataset_selectors[0], trace.trace_selector):
        if workload.row_offset:
            raise ValueError(
                "--dataset-offset is not supported when trace and dataset select the same source"
            )
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
        replace(
            event,
            request=replace(
                request, metadata={**request.metadata, **(event.metadata or {})}
            ),
            request_origin=request_origin,
        )
        for event, request in zip(events, requests)
    ]
