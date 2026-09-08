# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""为随机负载和 Mooncake 轨迹生成确定性的 token 形状提示词。"""

from __future__ import annotations

import logging
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

# 远程 tokenizer 只下载分词与解码所需文件。
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
    """解析本地 tokenizer 目录，或从 Hub 下载必要文件。"""
    local = Path(tokenizer_path).expanduser()
    if local.exists():
        return str(local.resolve())

    logger.info("Resolving tokenizer from Hugging Face repo %r", tokenizer_path)
    return snapshot_download(
        repo_id=tokenizer_path,
        allow_patterns=list(_TOKENIZER_ALLOW_PATTERNS),
    )


def _load_tokenizer(tokenizer_path: str) -> Any:
    return AutoTokenizer.from_pretrained(resolve_tokenizer_path(tokenizer_path))


def _allowed_token_ids(tokenizer: Any) -> list[int]:
    """返回可稳定解码为合成文本的非特殊 token。"""
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
    """拥有一个随机负载点的 tokenizer、随机状态和共享前缀。"""

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
        """为一个负载点采样闭区间内的内部提示词长度。"""
        return [
            int(value)
            for value in self.rng.integers(minimum, maximum + 1, size=count)
        ]

    def generate_requests(self, input_lengths: list[int]) -> list[ChatRequestContent]:
        """按给定内部长度生成共享同一前缀的聊天请求内容。"""
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
    """为普通负载或到达轨迹生成确定性随机请求内容。"""
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
    """根据 Mooncake hash ID 构造可重复的 512-token 前缀块。"""
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
