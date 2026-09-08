# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Send and measure OpenAI-compatible Chat Completions requests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from openai import APIError, AsyncOpenAI

from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.request_metrics import compute_tpot


@dataclass(frozen=True)
class ChatRequestContent:
    """Represent one independent Chat Completions request without owning episode state."""

    prompt: str | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None


def _openai_base_url(chat_completions_url: str) -> str:
    return chat_completions_url.rstrip("/").removesuffix("/chat/completions")


class ChatCompletionsLoadClient:
    """Own the Chat Completions client and generation settings for one HTTP workload point."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        *,
        max_concurrency: int,
        request_count: int,
    ) -> None:
        self._generation = benchmark.generation
        self._request_overrides = benchmark.generation.request_overrides()
        connection_limit = (
            request_count
            if benchmark.load_schedule.unbounded_concurrency
            else max_concurrency
        )
        limits = httpx.Limits(
            max_connections=connection_limit,
            max_keepalive_connections=connection_limit,
        )
        endpoint = benchmark.endpoint
        # Each measured request must map to one service request; retries change arrival rate, failure rate, and latency.
        self._client = AsyncOpenAI(
            base_url=_openai_base_url(endpoint.url),
            api_key=endpoint.api_key,
            max_retries=0,
            default_headers=endpoint.headers,
            http_client=httpx.AsyncClient(
                timeout=endpoint.timeout_seconds,
                limits=limits,
            ),
        )
        self._model = endpoint.model

    async def __aenter__(self) -> ChatCompletionsLoadClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.close()

    async def send(self, request: ChatRequestContent) -> dict[str, Any]:
        """Send one independent chat request and return its benchmark observations."""
        messages = request.messages
        if messages is None:
            if request.prompt is None:
                raise ValueError("Either prompt or messages must be provided")
            messages = [{"role": "user", "content": request.prompt}]
        stream = self._generation.stream
        request_fields: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._generation.sample_max_tokens(),
            "stream": stream,
        }
        if self._request_overrides:
            request_fields["extra_body"] = self._request_overrides
        if stream:
            request_fields["stream_options"] = {"include_usage": True}
        if request.tools:
            request_fields["tools"] = request.tools

        started_at = time.perf_counter()
        ttft: Optional[float] = None
        input_tokens = output_tokens = 0
        status_code: Optional[int] = None
        error_message: Optional[str] = None
        success = True
        try:
            response = await self._client.chat.completions.create(**request_fields)
            status_code = httpx.codes.OK
            if stream:
                async for chunk in response:
                    if chunk.usage is not None:
                        input_tokens = int(chunk.usage.prompt_tokens)
                        output_tokens = int(chunk.usage.completion_tokens)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if (delta.content or delta.tool_calls) and ttft is None:
                        ttft = time.perf_counter() - started_at
            elif response.usage is not None:
                input_tokens = int(response.usage.prompt_tokens)
                output_tokens = int(response.usage.completion_tokens)
        except (APIError, httpx.HTTPError) as exc:
            success = False
            status_code = getattr(exc, "status_code", None)
            error_message = str(exc)

        latency = time.perf_counter() - started_at
        # TTFT and TPOT are defined only for streamed token arrivals.
        if not stream:
            ttft = None
        return {
            "success": success,
            "status_code": status_code,
            "stream": stream,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, output_tokens),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error_message,
        }
