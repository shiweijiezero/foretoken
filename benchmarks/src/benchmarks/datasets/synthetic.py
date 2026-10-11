# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare reproducible random inputs independently of scheduling and warmup."""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.datasets.random_dataset import RandomDatasetPlugin

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import Task, parse_message_turns
from benchmarks.datasets.huggingface import resolve_tokenizer_path
from benchmarks.datasets.traces import MOONCAKE_BLOCK_TOKENS
from benchmarks.model_service import ModelService


class RandomRequestSource(RandomDatasetPlugin):
    """Reuse EvalScope token filtering and generation with restartable, local sampling."""

    def __init__(self, benchmark: BenchmarkConfig, service: ModelService) -> None:
        service = replace(service, model=benchmark.generation.extra_body.get("model", service.model))
        self.workload = benchmark.resolved_workload
        self._prefix_rng = random.Random(self.workload.random_seed)
        tokenizer = self.workload.tokenizer
        if not tokenizer:
            source, tokenizer = service.tokenizer_identity
            tokenizer = resolve_tokenizer_path(tokenizer, source=source)
        super().__init__(
            Arguments(
                model=service.model,
                url=service.chat_completions_url,
                api="openai",
                number=1,
                parallel=1,
                dataset="random",
                tokenizer_path=resolve_tokenizer_path(tokenizer),
                min_prompt_length=self.workload.minimum_prompt_tokens,
                max_prompt_length=self.workload.maximum_prompt_tokens,
                prefix_length=self.workload.shared_prefix_tokens,
                apply_chat_template=self.workload.apply_chat_template,
                tokenize_prompt=not self.workload.apply_chat_template,
                visualizer=None,
            )
        )
        self.minimum, self.maximum = self._resolve_prompt_length_bounds()

    def get_random_inputs(self, length: int) -> list[int]:
        """Generate the plugin's shared prefix without changing process-global random state."""
        return [
            int(
                self.allowed_tokens[
                    self._prefix_rng.randrange(len(self.allowed_tokens))
                ]
            )
            for _ in range(length)
        ]

    def requests(self, input_lengths: list[int] | None = None) -> Iterator[Task]:
        """Start the same input sequence for finite, duration, and warmup phases."""
        rng = random.Random(self.workload.random_seed)
        lengths = input_lengths if input_lengths is not None else itertools.repeat(None)
        template_length = (
            self.get_template_len() if self.workload.apply_chat_template else 0
        )
        for index, recorded_length in enumerate(lengths):
            length = rng.randrange(self.minimum, self.maximum)
            if recorded_length is not None:
                length = recorded_length - self.prefix_length - template_length
                if length < 0:
                    raise ValueError(
                        "Trace input_length is shorter than the configured prefix and chat template"
                    )
            offset = (
                rng.randrange(len(self.allowed_tokens)) + self.workload.row_offset
            ) % len(self.allowed_tokens)
            if self.workload.apply_chat_template:
                # The pinned plugin scopes its text-repair RNG to this per-item seed.
                message, _ = self._build_random_message(
                    length, offset, index, rng.randrange(2**32)
                )
                yield Task(
                    id=f"random:{index}",
                    turns=parse_message_turns(message, Path("random"), index + 1),
                )
            else:
                tokens = self.generate_token_ids_only(length, offset, index)
                if not tokens:
                    raise ValueError("Random input must contain at least one token")
                yield Task(
                    id=f"random:{index}", turns=(), prompt_token_ids=tuple(tokens)
                )


def generate_trace_random_requests(
    benchmark: BenchmarkConfig,
    service: ModelService,
    *,
    request_count: int,
    input_lengths: list[int] | None = None,
) -> list[Task]:
    """Prepare one request per trace event, honoring recorded total input lengths."""
    source = RandomRequestSource(benchmark, service)
    return list(itertools.islice(source.requests(input_lengths), request_count))


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

    plugin = RandomRequestSource(benchmark, service)
    allowed_token_ids = [int(token_id) for token_id in plugin.allowed_tokens]

    @lru_cache(maxsize=1024)
    def block_for(hash_id: int) -> tuple[int, ...]:
        generator = random.Random(workload.random_seed + hash_id)
        return tuple(
            generator.choice(allowed_token_ids) for _ in range(MOONCAKE_BLOCK_TOKENS)
        )

    tasks: list[Task] = []
    for index, (input_length, hash_ids) in enumerate(zip(input_lengths, hash_id_lists)):
        if hash_ids is None:
            raise ValueError(
                "--trace-synthetic-prefix-reuse requires hash_ids on every "
                "selected trace event"
            )
        expected_blocks = (
            input_length + MOONCAKE_BLOCK_TOKENS - 1
        ) // MOONCAKE_BLOCK_TOKENS
        if len(hash_ids) != expected_blocks:
            raise ValueError(
                f"Mooncake hash_ids must cover every {MOONCAKE_BLOCK_TOKENS}-token input block; "
                f"got {len(hash_ids)} hash_ids for input_length={input_length}"
            )

        prompt_token_ids = [
            token_id for hash_id in hash_ids for token_id in block_for(hash_id)
        ][:input_length]
        if not prompt_token_ids:
            raise ValueError("Synthetic prefix reuse produced an empty token payload")
        tasks.append(
            Task(
                id=f"prefix-reuse:{index}",
                turns=(),
                prompt_token_ids=tuple(prompt_token_ids),
            )
        )
    return tasks
