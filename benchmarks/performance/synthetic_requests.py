# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Generate deterministic token-shaped prompts for random workloads and Mooncake traces."""

from __future__ import annotations

import logging
import os
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

from benchmarks.performance.chat_client import ChatRequestContent
from benchmarks.performance.benchmark_config import HttpBenchmarkConfig

logger = logging.getLogger(__name__)
_MOONCAKE_BLOCK_TOKENS = 512
_BYTE_FALLBACK_TOKEN = re.compile(r"<0x[0-9A-Fa-f]{2}>")

# Remote tokenizers download only files needed for tokenization and decoding.
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


def _configured_hub_cache_dir() -> str | None:
    """Return the cache directory selected by the current runtime environment."""
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(variable)
        if value:
            return str(Path(value).expanduser())

    home = os.environ.get("HF_HOME")
    if home:
        return str(Path(home).expanduser() / "hub")

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return str(Path(xdg_cache).expanduser() / "huggingface" / "hub")
    return None


def resolve_tokenizer_path(tokenizer_path: str) -> str:
    """Resolve a local tokenizer or download a Hub tokenizer at runtime."""
    local = Path(tokenizer_path).expanduser()
    if local.exists():
        return str(local.resolve())
    if local.is_absolute() or tokenizer_path.startswith(("./", "../", "~")):
        raise ValueError(
            f"Tokenizer path does not exist locally: {tokenizer_path!r}; "
            "pass an existing directory or a Hugging Face repository ID"
        )

    cache_dir = _configured_hub_cache_dir()
    logger.info(
        "Resolving tokenizer from Hugging Face repo %r%s",
        tokenizer_path,
        f" into {cache_dir!r}" if cache_dir else "",
    )
    download_args: dict[str, Any] = {
        "repo_id": tokenizer_path,
        "allow_patterns": list(_TOKENIZER_ALLOW_PATTERNS),
    }
    if cache_dir:
        # Pass the runtime-selected directory explicitly.  Hugging Face reads
        # its default cache at import time, which can retain a path inherited
        # from the machine that launched a remote benchmark.
        download_args["cache_dir"] = cache_dir
    return snapshot_download(**download_args)


def _load_tokenizer(tokenizer_path: str) -> Any:
    return AutoTokenizer.from_pretrained(resolve_tokenizer_path(tokenizer_path))


def _allowed_token_ids(tokenizer: Any) -> list[int]:
    """Return non-special tokens that decode reliably into synthetic text."""
    special_ids = set(tokenizer.all_special_ids)
    allowed = []
    for token_id in range(len(tokenizer)):
        if token_id in special_ids:
            continue
        token = tokenizer.convert_ids_to_tokens(token_id)
        if not isinstance(token, str) or _BYTE_FALLBACK_TOKEN.fullmatch(token):
            continue
        allowed.append(token_id)
    if not allowed:
        raise ValueError("Tokenizer has no usable non-special tokens")
    return allowed


def _decode_prompt(tokenizer: Any, token_ids: list[int]) -> str:
    if not token_ids:
        return ""
    prompt = tokenizer.decode(
        token_ids,
        skip_special_tokens=False,
        clean_up_tokenization_spaces=False,
    )
    if not prompt:
        raise ValueError("Tokenizer produced an empty random prompt")
    return prompt


class RandomChatRequestGenerator:
    """Own the tokenizer, random state, and shared prefix for one random workload point."""

    def __init__(
        self,
        tokenizer_ref: str,
        *,
        seed: int,
        row_offset: int,
        shared_prefix_tokens: int,
    ) -> None:
        self.tokenizer = _load_tokenizer(tokenizer_ref)
        self.allowed_token_ids = _allowed_token_ids(self.tokenizer)
        self.row_offset = row_offset
        self.rng = np.random.default_rng(seed)
        self.prefix_ids = self._sample_token_ids(shared_prefix_tokens)

    def _sample_token_ids(self, count: int) -> list[int]:
        if count <= 0:
            return []
        indexes = self.rng.integers(
            0,
            len(self.allowed_token_ids),
            size=count,
        )
        return [self.allowed_token_ids[int(index)] for index in indexes]

    def sample_prompt_lengths(
        self,
        count: int,
        minimum: int,
        maximum: int,
    ) -> list[int]:
        """Sample inner prompt lengths from an inclusive range for one workload point."""
        return [
            int(value)
            for value in self.rng.integers(minimum, maximum + 1, size=count)
        ]

    def generate_requests(self, input_lengths: list[int]) -> list[ChatRequestContent]:
        """Generate chat request content with a shared prefix for the given inner lengths."""
        vocabulary_size = len(self.allowed_token_ids)
        requests: list[ChatRequestContent] = []
        for index, input_length in enumerate(input_lengths):
            if input_length < 0:
                raise ValueError(f"input length must be >= 0, got {input_length}")
            random_offset = int(self.rng.integers(0, vocabulary_size))
            start = (random_offset + self.row_offset + index) % vocabulary_size
            body_ids = [
                self.allowed_token_ids[(start + position) % vocabulary_size]
                for position in range(input_length)
            ]
            prompt = _decode_prompt(self.tokenizer, self.prefix_ids + body_ids)
            requests.append(ChatRequestContent(prompt=prompt))
        return requests


def generate_random_requests(
    benchmark: HttpBenchmarkConfig,
    *,
    request_count: int | None = None,
    input_lengths: list[int] | None = None,
) -> list[ChatRequestContent]:
    """Generate deterministic random request content for a standard workload or arrival trace."""
    dataset = benchmark.request_dataset
    if not dataset.tokenizer:
        raise ValueError("tokenizer_path is required for random data generation")

    generator = RandomChatRequestGenerator(
        dataset.tokenizer,
        seed=dataset.random_seed,
        row_offset=dataset.row_offset,
        shared_prefix_tokens=dataset.shared_prefix_tokens,
    )
    if input_lengths is None:
        count = (
            benchmark.load_schedule.request_count
            if request_count is None
            else request_count
        )
        input_lengths = generator.sample_prompt_lengths(
            count,
            dataset.minimum_prompt_tokens,
            dataset.maximum_prompt_tokens,
        )
    elif request_count is not None and request_count != len(input_lengths):
        raise ValueError("request_count must match input_lengths")
    return generator.generate_requests(input_lengths)


def generate_synthetic_prefix_reuse_requests(
    benchmark: HttpBenchmarkConfig,
    *,
    input_lengths: list[int],
    hash_id_lists: list[list[int] | None],
) -> list[ChatRequestContent]:
    """Build reproducible 512-token prefix blocks from Mooncake hash IDs."""
    dataset = benchmark.request_dataset
    if not dataset.tokenizer:
        raise ValueError("tokenizer_path is required for random data generation")
    if len(input_lengths) != len(hash_id_lists):
        raise ValueError("input_lengths must match hash_id_lists")

    tokenizer = _load_tokenizer(dataset.tokenizer)
    allowed_token_ids = _allowed_token_ids(tokenizer)

    @lru_cache(maxsize=1024)
    def block_for(hash_id: int) -> tuple[int, ...]:
        generator = random.Random(dataset.random_seed + hash_id)
        return tuple(
            generator.choice(allowed_token_ids)
            for _ in range(_MOONCAKE_BLOCK_TOKENS)
        )

    requests: list[ChatRequestContent] = []
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
        requests.append(
            ChatRequestContent(prompt=_decode_prompt(tokenizer, prompt_token_ids))
        )
    return requests
