# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Collect complete next-token probabilities through the native Completions API."""

from __future__ import annotations

import math
from typing import Iterable

import httpx
import numpy as np

from benchmarks.model_service import ModelService


class CompletionDistributionClient:
    """Own one service connection while scoring fixed token-ID prefixes, not generated histories."""

    def __init__(self, service: ModelService, *, timeout: float) -> None:
        self._model = service.model
        self._url = service.api_root + "/completions"
        self._client = httpx.Client(
            headers={**service.request_headers, "Authorization": f"Bearer {service.api_key}"},
            timeout=timeout,
        )

    def __enter__(self) -> CompletionDistributionClient:
        return self

    def __exit__(self, *args: object) -> None:
        self._client.close()

    def logprobs(
        self, prefix: list[int], vocabulary_size: int
    ) -> np.ndarray:
        """Return normalized log probabilities indexed by token ID for the next position.

        Full-vocabulary output and token-ID keys are vLLM public API extensions.
        Checking vocabulary coverage prevents silently interpreting top-k output
        or colliding decoded-token strings as a complete probability distribution.
        """
        response = self._client.post(self._url, json={
            "model": self._model,
            "prompt": prefix,
            "max_tokens": 1,
            "logprobs": vocabulary_size,
            "return_tokens_as_token_ids": True,
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 0,
            "min_p": 0.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "stream": False,
        })
        if response.is_error:
            raise ValueError(
                f"Distribution scoring for {self._model!r} returned HTTP {response.status_code}. "
                "The service must support full-vocabulary logprobs and return_tokens_as_token_ids; "
                "for vLLM, configure max-logprobs=-1."
            )
        payload = response.json()
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
    token_index: int,
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
        "reference_token_delta_p": float(q[token_index] - p[token_index]),
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
