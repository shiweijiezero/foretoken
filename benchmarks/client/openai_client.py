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
    return url.rstrip("/").removesuffix("/chat/completions")


def derive_max_connections(
    *, parallel: int, number: int, open_loop: bool
) -> int:
    """Size the httpx pool so it does not throttle below the load model.

    Closed-loop: in-flight ≤ ``parallel``.
    Open-loop: up to ``number`` may be in flight (gather / paced fire).
    """
    return number if open_loop else parallel


class OpenAICompatClient:
    """OpenAI-compatible chat client."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: int,
        api_key: str,
        max_connections: int,
        max_retries: int,
        headers: dict[str, str] | None = None,
    ):
        self.model = model
        # Keepalive matches max so finished requests can be reused under the
        # same concurrency budget; the client is closed at end of each run.
        self.client = AsyncOpenAI(
            base_url=_base_url(url),
            api_key=api_key,
            max_retries=max_retries,
            default_headers=headers,
            http_client=httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                ),
            ),
        )

    async def close(self) -> None:
        await self.client.close()

    async def generate(
        self,
        max_tokens: int,
        *,
        stream: bool,
        extra_body: dict[str, Any],
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
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = tools

        start_time = time.perf_counter()
        ttft: Optional[float] = None
        input_tokens = output_tokens = 0
        status: Optional[int] = None
        error_message: Optional[str] = None
        success = True
        try:
            response = await self.client.chat.completions.create(**kwargs)
            status = httpx.codes.OK
            if stream:
                async for chunk in response:
                    if chunk.usage is not None:
                        input_tokens = int(chunk.usage.prompt_tokens)
                        output_tokens = int(chunk.usage.completion_tokens)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if (delta.content or delta.tool_calls) and ttft is None:
                        ttft = time.perf_counter() - start_time
            elif response.usage is not None:
                input_tokens = int(response.usage.prompt_tokens)
                output_tokens = int(response.usage.completion_tokens)
        except Exception as exc:
            success = False
            status = getattr(exc, "status_code", None)
            error_message = str(exc)

        latency = time.perf_counter() - start_time
        return {
            "success": success,
            "status_code": status,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, output_tokens),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error_message,
        }
