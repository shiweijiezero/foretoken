# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Generate random-token and prefix-reuse request tasks with EvalScope's random dataset plugin."""

from __future__ import annotations

import random
from functools import lru_cache
from itertools import islice
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.datasets.conversations import Task, Turn
from benchmarks.datasets.huggingface import resolve_tokenizer_path

_MOONCAKE_BLOCK_TOKENS = 512


def create_trace_random_dataset_plugin(
    benchmark: BenchmarkConfig,
    service: ModelService,
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

    workload = benchmark.resolved_workload
    seed_everything(workload.random_seed)
    arguments = Arguments(
        model=service.model,
        url=service.chat_completions_url,
        api="openai",
        number=request_count,
        parallel=1,
        dataset="random",
        tokenizer_path=resolve_tokenizer_path(workload.tokenizer),
        dataset_offset=workload.row_offset,
        min_prompt_length=workload.minimum_prompt_tokens,
        max_prompt_length=workload.maximum_prompt_tokens,
        prefix_length=workload.shared_prefix_tokens,
        apply_chat_template=False,
        visualizer=None,
    )
    return RandomDatasetPlugin(arguments)


def _random_task(message: Any, index: int) -> Task:
    """Convert one public EvalScope random message into a request task."""
    if isinstance(message, str):
        return Task(id=f"random:{index}", turns=(Turn(role="user", content=message),))
    if isinstance(message, list) and all(
        isinstance(item, dict) for item in message
    ):
        return Task(
            id=f"random:{index}",
            turns=tuple(
                Turn(role=str(item["role"]), content=item["content"])
                for item in message
            ),
        )
    raise TypeError(
        "EvalScope random dataset returned unsupported message type "
        f"{type(message).__name__}"
    )


def generate_trace_random_requests(
    benchmark: BenchmarkConfig,
    service: ModelService,
    *,
    request_count: int,
    input_lengths: list[int] | None = None,
) -> list[Task]:
    """Generate trace random payloads, preserving recorded per-request lengths when present."""
    plugin = create_trace_random_dataset_plugin(
        benchmark,
        service,
        request_count,
    )
    if input_lengths is None:
        messages = list(islice(plugin.build_messages(), request_count))
    else:
        if request_count != len(input_lengths):
            raise ValueError("request_count must match input_lengths")
        if any(length < 0 for length in input_lengths):
            raise ValueError("trace input lengths must be >= 0")
        offset = benchmark.resolved_workload.row_offset
        messages = [
            plugin.generate_token_sequence(length, offset, index)[0]
            for index, length in enumerate(input_lengths)
        ]
    if len(messages) != request_count:
        raise ValueError(
            f"EvalScope generated {len(messages)} random requests; need {request_count}"
        )
    return [_random_task(message, index) for index, message in enumerate(messages)]


def generate_synthetic_prefix_reuse_requests(
    benchmark: BenchmarkConfig,
    service: ModelService,
    *,
    input_lengths: list[int],
    hash_id_lists: list[list[int] | None],
) -> list[Task]:
    """Build reproducible 512-token prefix blocks from Mooncake hash IDs."""
    workload = benchmark.resolved_workload
    if len(input_lengths) != len(hash_id_lists):
        raise ValueError("input_lengths must match hash_id_lists")

    plugin = create_trace_random_dataset_plugin(
        benchmark,
        service,
        len(input_lengths),
    )
    tokenizer = plugin.tokenizer
    allowed_token_ids = [int(token_id) for token_id in plugin.allowed_tokens]

    @lru_cache(maxsize=1024)
    def block_for(hash_id: int) -> tuple[int, ...]:
        generator = random.Random(workload.random_seed + hash_id)
        return tuple(
            generator.choice(allowed_token_ids)
            for _ in range(_MOONCAKE_BLOCK_TOKENS)
        )

    tasks: list[Task] = []
    for index, (input_length, hash_ids) in enumerate(zip(input_lengths, hash_id_lists)):
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
        tasks.append(
            Task(id=f"prefix-reuse:{index}", turns=(Turn(role="user", content=prompt),))
        )
    return tasks
