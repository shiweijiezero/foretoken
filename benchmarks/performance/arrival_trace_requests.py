# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""把到达轨迹事件绑定到最终的独立聊天请求。"""

from __future__ import annotations

from dataclasses import replace

from benchmarks.performance.arrival_trace_records import (
    ArrivalTraceEvent,
    ArrivalTraceReader,
)
from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.huggingface_datasets import same_dataset_selector
from benchmarks.performance.synthetic_requests import (
    generate_random_requests,
    generate_synthetic_prefix_reuse_requests,
)
from benchmarks.performance.request_datasets import (
    load_chat_requests,
    load_indexed_chat_requests,
)


def _request_origin(benchmark: HttpBenchmarkConfig) -> str:
    return (
        "random"
        if benchmark.request_dataset.dataset_selectors == ["random"]
        else "dataset"
    )


def bind_arrival_trace_requests(
    benchmark: HttpBenchmarkConfig,
    events: list[ArrivalTraceEvent],
) -> tuple[str, list[ArrivalTraceEvent]]:
    """为所选到达事件绑定原生、随机或外部数据集请求。"""
    request_origin = _request_origin(benchmark)
    dataset = benchmark.request_dataset
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
                input_lengths=[int(length) for length in input_lengths],
                hash_id_lists=[event.hash_ids for event in events],
            )
        else:
            requests = generate_random_requests(
                benchmark,
                request_count=len(events),
                input_lengths=(
                    [int(length) for length in input_lengths]
                    if all(has_input_lengths)
                    else None
                ),
            )
    elif same_dataset_selector(
        dataset.dataset_selectors[0],
        trace.trace_selector,
    ):
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


def load_arrival_trace_requests(
    benchmark: HttpBenchmarkConfig,
) -> tuple[float, str, str, list[ArrivalTraceEvent]]:
    """读取轨迹窗口并为每个到达事件绑定最终聊天请求。"""
    trace = benchmark.arrival_trace
    reader = ArrivalTraceReader(trace.trace_selector)
    trace_window_start, events = reader.read_window(
        start_offset_seconds=trace.start_offset_seconds,
        duration_seconds=trace.duration_seconds,
    )
    request_origin, bound_events = bind_arrival_trace_requests(
        benchmark, events
    )
    if reader.trace_format is None:
        raise RuntimeError("Trace format was not detected")
    return (
        trace_window_start,
        reader.trace_format,
        request_origin,
        bound_events,
    )
