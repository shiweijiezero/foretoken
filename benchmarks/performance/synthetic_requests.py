# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Adapt EvalScope random prompts for trace-specific request payloads."""

from __future__ import annotations

from itertools import islice
from typing import Any

from benchmarks.performance.config import HttpBenchmarkConfig
from benchmarks.performance.conversation import (
    ChatRequestContent,
    resolve_tokenizer_path,
)
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint


def create_trace_random_dataset_plugin(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    request_count: int,
) -> Any:
    """Create EvalScope's public random dataset plugin for one trace payload set."""
    try:
        from evalscope.perf.arguments import Arguments
        from evalscope.perf.plugin.datasets.random_dataset import RandomDatasetPlugin
        from evalscope.utils.model_utils import seed_everything
    except ModuleNotFoundError as error:
        raise ValueError(
            "random trace payloads require EvalScope; install benchmark "
            "dependencies with: pip install 'foretoken[bench]'"
        ) from error

    dataset = benchmark.request_dataset
    seed_everything(dataset.random_seed)
    arguments = Arguments(
        model=endpoint.model,
        url=endpoint.url,
        api="openai",
        number=request_count,
        parallel=1,
        dataset="random",
        tokenizer_path=resolve_tokenizer_path(dataset.tokenizer),
        dataset_offset=dataset.row_offset,
        min_prompt_length=dataset.minimum_prompt_tokens,
        max_prompt_length=dataset.maximum_prompt_tokens,
        prefix_length=dataset.shared_prefix_tokens,
        apply_chat_template=False,
        visualizer=None,
    )
    return RandomDatasetPlugin(arguments)


def _chat_request_content(message: Any) -> ChatRequestContent:
    """Convert one public EvalScope random message into trace request content."""
    if isinstance(message, str):
        return ChatRequestContent(prompt=message)
    if isinstance(message, list) and all(
        isinstance(item, dict) for item in message
    ):
        return ChatRequestContent(messages=message)
    raise TypeError(
        "EvalScope random dataset returned unsupported message type "
        f"{type(message).__name__}"
    )


def generate_trace_random_requests(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    *,
    request_count: int,
    input_lengths: list[int] | None = None,
) -> list[ChatRequestContent]:
    """Generate trace random payloads, preserving recorded per-request lengths when present."""
    plugin = create_trace_random_dataset_plugin(
        benchmark,
        endpoint,
        request_count,
    )
    if input_lengths is None:
        messages = list(islice(plugin.build_messages(), request_count))
    else:
        if request_count != len(input_lengths):
            raise ValueError("request_count must match input_lengths")
        if any(length < 0 for length in input_lengths):
            raise ValueError("trace input lengths must be >= 0")
        offset = benchmark.request_dataset.row_offset
        messages = [
            plugin.generate_token_sequence(length, offset, index)[0]
            for index, length in enumerate(input_lengths)
        ]
    if len(messages) != request_count:
        raise ValueError(
            f"EvalScope generated {len(messages)} random requests; need {request_count}"
        )
    return [_chat_request_content(message) for message in messages]
