# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""OpenAI-compatible chat client."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI

from benchmarks.metrics.aggregator import compute_tpot


def _base_url(url: str) -> str:
    u = url.rstrip("/")
    return u.removesuffix("/chat/completions") or u


def derive_max_connections(
    *, parallel: int, number: int, open_loop: bool
) -> int:
    """Size the httpx pool so it does not throttle below the load model.

    Closed-loop: in-flight ≤ ``parallel``.
    Open-loop: up to ``number`` may be in flight (gather / paced fire).
    """
    return max(number if open_loop else parallel, 1)


class OpenAICompatClient:
    """OpenAI-compatible chat client."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: int,
        api_key: str,
        max_connections: int,
    ):
        self.model = model
        # Keepalive matches max so finished requests can be reused under the
        # same concurrency budget; the client is closed at end of each run.
        self.client = AsyncOpenAI(
            base_url=_base_url(url),
            api_key=api_key or "EMPTY",
            max_retries=2,
            http_client=httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                ),
            ),
        )

    async def start(self) -> None:
        """No-op; client is ready after ``__init__`` (kept for runner API)."""

    async def close(self) -> None:
        await self.client.close()

    async def generate_stream(
        self,
        max_tokens: int,
        temperature: float,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if messages is None:
            if prompt is None:
                raise ValueError("Either prompt or messages must be provided")
            messages = [{"role": "user", "content": prompt}]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        t0 = time.perf_counter()
        ttft: Optional[float] = None
        in_tok = out_tok = n_content = 0
        status: Optional[int] = None
        error: Optional[str] = None
        success = True
        try:
            resp = await self.client.chat.completions.create(**kwargs)
            status = httpx.codes.OK
            async for chunk in resp:
                if chunk.usage:
                    in_tok = int(chunk.usage.prompt_tokens or in_tok)
                    out_tok = int(chunk.usage.completion_tokens or out_tok)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content or delta.tool_calls:
                    ttft = ttft if ttft is not None else time.perf_counter() - t0
                    if delta.content:
                        n_content += 1
        except Exception as e:
            success = False
            status = getattr(e, "status_code", status)
            error = str(e)

        latency = time.perf_counter() - t0
        out_tok = out_tok or n_content
        return {
            "success": success,
            "status_code": status,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, out_tok),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "error": error,
        }
