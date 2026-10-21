# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Collect complete next-token probabilities through the native Completions API."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Self

import numpy as np
from openai import APIStatusError, OpenAI

from benchmarks.model_service import ModelService


class CompletionComparisonClient:
    """Own one Completions connection for fixed-prefix probability and generation comparisons."""

    def __init__(self, service: ModelService, *, timeout: float) -> None:
        self._model = service.model
        self._client = OpenAI(
            base_url=service.api_root,
            api_key=service.api_key,
            default_headers=service.request_headers,
            timeout=timeout,
            max_retries=0,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self._client.close()

    def generate(self, prefix: list[int], max_tokens: int) -> dict[str, object]:
        """Return the service's native generated IDs, text, and finish reason for a greedy continuation."""
        response = self._client.completions.create(
            model=self._model,
            prompt=prefix,
            max_tokens=max_tokens,
            temperature=0,
            stream=False,
            extra_body={"return_token_ids": True},
        )
        payload = response.model_dump()
        try:
            choice = payload["choices"][0]
            token_ids = choice["token_ids"]
            text = choice["text"]
            reason = choice["finish_reason"]
            if (not isinstance(token_ids, list) or any(
                not isinstance(token, int) or isinstance(token, bool) or token < 0
                for token in token_ids
            ) or not isinstance(text, str) or not isinstance(reason, str) or not reason):
                raise ValueError("missing native token IDs, generated text, or finish reason")
            if reason not in {"stop", "length"}:
                raise ValueError(f"generation did not finish normally: {reason}")
            return {"token_ids": token_ids, "text": text, "finish_reason": reason}
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid greedy completion for {self._model!r}: {error}; "
                "the service must return choices[0].token_ids and finish_reason"
            ) from error

    def logprobs(
        self, prefix: list[int], vocabulary_size: int
    ) -> np.ndarray:
        """Return normalized log probabilities indexed by token ID for the next position.

        Full-vocabulary output and token-ID keys are vLLM public API extensions.
        Checking vocabulary coverage prevents silently interpreting top-k output
        or colliding decoded-token strings as a complete probability distribution.
        """
        try:
            response = self._client.completions.create(
                model=self._model,
                prompt=prefix,
                max_tokens=1,
                logprobs=vocabulary_size,
                temperature=1.0,
                top_p=1.0,
                frequency_penalty=0.0,
                presence_penalty=0.0,
                stream=False,
                extra_body={
                    "return_tokens_as_token_ids": True,
                    "top_k": 0,
                    "min_p": 0.0,
                    "repetition_penalty": 1.0,
                },
            )
        except APIStatusError as error:
            raise ValueError(
                f"Distribution scoring for {self._model!r} returned HTTP {error.status_code}. "
                "The service must support full-vocabulary logprobs and return_tokens_as_token_ids; "
                "for vLLM, configure max-logprobs=-1."
            ) from error
        payload = response.model_dump()
        try:
            distribution = payload["choices"][0]["logprobs"]["top_logprobs"][0]
            if not isinstance(distribution, dict) or not distribution:
                raise ValueError("empty distribution")
            items = []
            for key, value in distribution.items():
                if not key.startswith("token_id:"):
                    raise ValueError("token-ID keys are required")
                token_id = int(key.removeprefix("token_id:"))
                logprob = float(value)
                if token_id < 0 or not math.isfinite(logprob):
                    raise ValueError("invalid token probability")
                items.append((token_id, logprob))
            items.sort()
            ids = np.asarray([item[0] for item in items], dtype=np.int64)
            values = np.asarray([item[1] for item in items], dtype=np.float64)
            if not np.array_equal(ids, np.arange(vocabulary_size)):
                raise ValueError("response does not cover the complete model vocabulary")
            mass = float(np.exp(values).sum())
            if not math.isclose(mass, 1.0, rel_tol=0.002, abs_tol=0.002):
                raise ValueError(f"full probability mass is {mass:.6g}, expected 1")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid full-vocabulary response for {self._model!r}: {error}") from error
        # Renormalize floating-point serialization error only after full-vocabulary
        # coverage and mass have been established.
        values -= np.logaddexp.reduce(values)
        return values


def compare_logprobs(
    reference: np.ndarray,
    candidate: np.ndarray,
    corpus_token_id: int,
    top_k: Iterable[int],
) -> dict[str, float | bool]:
    """Compare aligned full-vocabulary distributions at one teacher-forced position."""
    p = np.exp(reference)
    q = np.exp(candidate)
    reference_top = int(np.argmax(reference))
    candidate_top = int(np.argmax(candidate))
    centered_difference = (reference - reference.mean()) - (candidate - candidate.mean())
    values: dict[str, float | bool] = {
        "kl": max(0.0, float(np.dot(p, reference - candidate))),
        "top1_match": reference_top == candidate_top,
        "corpus_token_delta_p": float(q[corpus_token_id] - p[corpus_token_id]),
        "centered_logit_rmse": float(np.sqrt(np.mean(centered_difference ** 2))),
        "total_variation": float(np.abs(p - q).sum() / 2),
    }
    # Stable sorting uses the common token-ID order to break probability ties.
    reference_order = np.argsort(-reference, kind="stable")
    candidate_order = np.argsort(-candidate, kind="stable")
    for k in top_k:
        if k > len(reference):
            raise ValueError(f"top-k={k} exceeds vocabulary size {len(reference)}")
        values[f"top{k}_overlap"] = len(set(reference_order[:k]) & set(candidate_order[:k])) / k
    return values
