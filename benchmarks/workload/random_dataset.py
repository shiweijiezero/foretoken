# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Generate random synthetic prompt requests for ``--dataset random``."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.datasets.random_dataset import RandomDatasetPlugin
from huggingface_hub import snapshot_download

from benchmarks.config import BenchConfig

logger = logging.getLogger(__name__)
_MOONCAKE_BLOCK_TOKENS = 512

# Hub file patterns kept when resolving a remote ``--tokenizer-path``.
_TOKENIZER_ALLOW_PATTERNS = (
    "tokenizer*",
    "vocab*",
    "merges*",
    "special_tokens_map*",
    "added_tokens*",
    "chat_template*",
    "tokenization*",
    "config.json",
)


def resolve_tokenizer_path(tokenizer_path: str) -> str:
    """Resolve ``tokenizer_path`` to a local directory.

    Existing local paths are returned as-is; Hugging Face repo ids are fetched
    with ``_TOKENIZER_ALLOW_PATTERNS`` only.
    """
    local = Path(tokenizer_path).expanduser()
    if local.exists():
        return str(local.resolve())

    logger.info(
        "Resolving tokenizer from Hugging Face repo %r",
        tokenizer_path,
    )
    return snapshot_download(
        repo_id=tokenizer_path,
        allow_patterns=list(_TOKENIZER_ALLOW_PATTERNS),
    )


def _build_perf_arguments(
    config: BenchConfig,
    *,
    tokenizer_path: str,
    number: int,
    min_prompt_length: int,
    max_prompt_length: int,
) -> Arguments:
    """Build the random-prompt generator arguments for this config."""
    dataset = config.dataset
    apply_chat = dataset.resolve_apply_chat_template(config.endpoint.url)
    return Arguments.model_construct(
        model=config.endpoint.model,
        url=config.endpoint.url,
        api_key=config.endpoint.api_key,
        dataset="random",
        tokenizer_path=tokenizer_path,
        number=number,
        parallel=config.load.parallel,
        rate=float(config.load.rate),
        open_loop=config.load.open_loop,
        min_prompt_length=min_prompt_length,
        max_prompt_length=max_prompt_length,
        prefix_length=dataset.prefix_length,
        dataset_offset=dataset.dataset_offset,
        apply_chat_template=apply_chat,
        tokenize_prompt=False,
        dataset_args={},
        seed=dataset.random_seed,
        num_workers=0,
        warmup_num=0,
        max_tokens=config.generation.max_tokens,
        temperature=config.generation.temperature,
        stream=config.generation.stream,
    )


def _to_request(message: Any) -> dict[str, Any]:
    """Normalize a generated message into a request dict."""
    if isinstance(message, list):
        if message and isinstance(message[0], int):
            # Raw token-id lists are not supported by the client yet.
            raise ValueError(
                "tokenize_prompt token-id lists are not supported yet"
            )
        return {"messages": message}
    if isinstance(message, str):
        return {"prompt": message}
    raise TypeError(f"Unexpected message type: {type(message)!r}")


def _generate_random_requests(
    config: BenchConfig,
    *,
    tokenizer_path: str,
    number: int,
    min_prompt_length: int,
    max_prompt_length: int,
) -> list[dict[str, Any]]:
    arguments = _build_perf_arguments(
        config,
        tokenizer_path=tokenizer_path,
        number=number,
        min_prompt_length=min_prompt_length,
        max_prompt_length=max_prompt_length,
    )
    return _collect_random_requests(RandomDatasetPlugin(arguments), number)


def _collect_random_requests(
    plugin: RandomDatasetPlugin,
    number: int,
) -> list[dict[str, Any]]:
    """Collect exactly ``number`` requests from an initialized plugin."""
    requests: list[dict[str, Any]] = []
    for message in plugin.build_messages():
        requests.append(_to_request(message))
        if len(requests) >= number:
            break
    if len(requests) < number:
        raise RuntimeError(
            f"Random dataset yielded {len(requests)} messages, need {number}"
        )
    return requests


def generate_random_requests(
    config: BenchConfig,
    *,
    number: int | None = None,
    input_lengths: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Build random requests, optionally matching per-request input lengths."""
    dataset = config.dataset
    if not dataset.tokenizer_path:
        raise ValueError(
            "tokenizer_path is required for random data generation"
        )

    np.random.seed(dataset.random_seed)
    tokenizer_path = resolve_tokenizer_path(dataset.tokenizer_path)
    if input_lengths is None:
        count = config.load.number if number is None else number
        return _generate_random_requests(
            config,
            tokenizer_path=tokenizer_path,
            number=count,
            min_prompt_length=dataset.min_prompt_length,
            max_prompt_length=dataset.max_prompt_length,
        )

    if not input_lengths:
        return []

    if number is not None and number != len(input_lengths):
        raise ValueError("number must match input_lengths")
    positions: dict[int, list[int]] = defaultdict(list)
    for index, input_length in enumerate(input_lengths):
        positions[input_length].append(index)

    arguments = _build_perf_arguments(
        config,
        tokenizer_path=tokenizer_path,
        number=len(input_lengths),
        min_prompt_length=min(input_lengths),
        max_prompt_length=max(input_lengths),
    )
    plugin = RandomDatasetPlugin(arguments)
    requests: list[dict[str, Any] | None] = [None] * len(input_lengths)
    for input_length, indexes in positions.items():
        plugin.query_parameters.min_prompt_length = input_length
        plugin.query_parameters.max_prompt_length = input_length
        plugin.number = len(indexes)
        generated = _collect_random_requests(plugin, len(indexes))
        for index, request in zip(indexes, generated):
            requests[index] = request
    if any(request is None for request in requests):
        raise RuntimeError("Random dataset generation returned missing requests")
    return [request for request in requests if request is not None]


def generate_synthetic_prefix_reuse_requests(
    config: BenchConfig,
    *,
    input_lengths: list[int],
    hash_id_lists: list[list[int] | None],
) -> list[dict[str, Any]]:
    """Build deterministic Mooncake-prefix payloads from ``hash_ids``.

    Each hash id maps to a fixed synthetic 512-token block. A request joins
    its blocks in trace order and truncates the final block to ``input_length``.
    """
    dataset = config.dataset
    if not dataset.tokenizer_path:
        raise ValueError(
            "tokenizer_path is required for random data generation"
        )
    if len(input_lengths) != len(hash_id_lists):
        raise ValueError("input_lengths must match hash_id_lists")

    from transformers import AutoTokenizer

    tokenizer_path = resolve_tokenizer_path(dataset.tokenizer_path)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
    vocab_size = int(tokenizer.vocab_size)
    special_ids = set(tokenizer.all_special_ids)
    allowed_token_ids = [
        token_id
        for token_id in range(vocab_size)
        if token_id not in special_ids
    ]
    if not allowed_token_ids:
        raise ValueError("Tokenizer has no non-special tokens")

    @lru_cache(maxsize=1024)
    def block_for(hash_id: int) -> tuple[int, ...]:
        generator = random.Random(dataset.random_seed + hash_id)
        return tuple(
            generator.choice(allowed_token_ids)
            for _ in range(_MOONCAKE_BLOCK_TOKENS)
        )

    requests: list[dict[str, Any]] = []
    for input_length, hash_ids in zip(input_lengths, hash_id_lists):
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
            raise ValueError(
                "Tokenizer produced an empty synthetic prefix-reuse prompt"
            )
        requests.append({"prompt": prompt})
    return requests
