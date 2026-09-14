# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Send measured Chat Completions requests through one OpenAI-compatible client."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from openai import APIError, AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.integrations.streaming import ChatStreamTiming
from benchmarks.results.metrics import compute_tpot
from benchmarks.datasets.conversations import Task


class ChatCompletionsLoadClient:
    """Own the Chat Completions client and generation settings for one workload point."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        max_connections: int,
    ) -> None:
        self._generation = benchmark.generation
        self._request_overrides = benchmark.generation.request_overrides()
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        # SDK retries remain inside the measured logical request latency.
        self._client = AsyncOpenAI(
            base_url=service.api_root,
            api_key=service.api_key,
            max_retries=benchmark.service.max_retries,
            default_headers=service.request_headers,
            http_client=httpx.AsyncClient(
                timeout=benchmark.service.timeout_seconds,
                limits=limits,
            ),
        )
        self._model = service.model
        self._request_url = service.chat_completions_url

    async def __aenter__(self) -> ChatCompletionsLoadClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.close()

    async def send(self, task: Task) -> dict[str, Any]:
        """Send one independent request for ``task`` and return its raw observation record."""
        stream = self._generation.stream
        target_length = self._generation.sample_output_length()
        request_fields: dict[str, Any] = {
            "model": self._model,
            "messages": task.messages(),
            "max_tokens": target_length if target_length is not None else self._generation.sample_max_tokens(),
            "stream": stream,
        }
        request_fields.update(self._request_overrides)
        if target_length is not None:
            request_fields["max_tokens"] = target_length
            request_fields.update(min_tokens=target_length, ignore_eos=True)
        if stream:
            request_fields["stream_options"] = {"include_usage": True}
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            if key in task.metadata and key not in self._request_overrides:
                request_fields[key] = task.metadata[key]

        started_at = time.perf_counter()
        timing = ChatStreamTiming()
        input_tokens = output_tokens = 0
        status_code: Optional[int] = None
        error_message: Optional[str] = None
        success = True
        try:
            response = await self._client.post(
                self._request_url,
                body=request_fields,
                cast_to=ChatCompletion,
                stream=stream,
                stream_cls=AsyncStream[ChatCompletionChunk],
            )
            status_code = httpx.codes.OK
            if stream:
                async for chunk in response:
                    received_at = time.perf_counter()
                    timing.observe(chunk.model_dump(exclude_none=True), received_at)
                    if chunk.usage is not None:
                        input_tokens = int(chunk.usage.prompt_tokens)
                        output_tokens = int(chunk.usage.completion_tokens)
            elif response.usage is not None:
                input_tokens = int(response.usage.prompt_tokens)
                output_tokens = int(response.usage.completion_tokens)
        except (APIError, httpx.HTTPError) as exc:
            success = False
            status_code = getattr(exc, "status_code", None)
            error_message = str(exc)

        if success and target_length is not None and output_tokens != target_length:
            success = False
            error_message = (
                f"Output length mismatch: requested {target_length} tokens, service reported {output_tokens}; "
                "verify min_tokens and ignore_eos support"
            )
        completed_at = (
            timing.last_output_at
            if stream and success and timing.last_output_at is not None
            else time.perf_counter()
        )
        latency = completed_at - started_at
        ttft = (
            timing.first_output_at - started_at
            if stream and timing.first_output_at is not None else None
        )
        return {
            "success": success,
            "status_code": status_code,
            "stream": stream,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, output_tokens),
            "inter_token_latencies": timing.intervals if stream else [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error_message,
        }
