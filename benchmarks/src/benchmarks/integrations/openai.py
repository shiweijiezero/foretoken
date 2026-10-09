# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Measure chat and tokenized text generation through one OpenAI-compatible client."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx
from openai import APIError, AsyncOpenAI, AsyncStream
from openai.types import Completion
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.datasets.conversations import Task
from benchmarks.integrations.streaming import ChatStreamTiming
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import compute_tpot


@dataclass(frozen=True)
class PreparedRequest:
    """An OpenAI request with resolved generation fields and usage expectations."""

    body: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None


class RequestBuilder:
    """Resolve request controls before scheduling, without owning transport or RNG state."""

    def __init__(self, benchmark: BenchmarkConfig, service: ModelService) -> None:
        self.generation = benchmark.generation
        self.overrides = self.generation.request_overrides()
        self.service = service

    def model_for(self, metadata: Mapping[str, Any]) -> str:
        """Resolve the advertised model used for request preparation and tokenization."""
        model = metadata.get("model", self.overrides.get("model", self.service.model))
        if not model:
            raise ValueError("Dataset row must specify model when --model is omitted")
        if (
            self.service.model_service_refs or self.service.deployment is not None
        ) and model not in self.service.models:
            raise ValueError(
                f"Dataset model {model!r} is not advertised by the deployment"
            )
        return model

    def prepare(
        self,
        task: Task,
        messages: list[dict[str, Any]],
        rng: random.Random,
        *,
        reference_length: int | None = None,
    ) -> PreparedRequest:
        """Apply row controls, sampled lengths, and reference lengths in that order."""
        generation = self.generation
        metadata = task.metadata
        target = metadata.get("output_length")
        if target is None and generation.min_output_length is not None:
            target = rng.randint(
                generation.min_output_length, generation.max_output_length
            )
        if target is None:
            target = reference_length
        limit = generation.max_tokens
        maximum = rng.randint(*limit) if isinstance(limit, list) else limit
        model = self.model_for(metadata)
        body = {
            "max_tokens": maximum,
            **self.overrides,
            "model": model,
            "stream": generation.stream,
            **(
                {"prompt": list(task.prompt_token_ids)}
                if task.prompt_token_ids is not None
                else {"messages": messages}
            ),
        }
        if "priority" in metadata:
            body["priority"] = metadata["priority"]
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            if key in metadata and key not in self.overrides:
                body[key] = metadata[key]
        if target is not None:
            body.update(max_tokens=target, min_tokens=target, ignore_eos=True)
        if generation.stream:
            body["stream_options"] = {"include_usage": True}
        return PreparedRequest(
            body,
            input_tokens=len(task.prompt_token_ids)
            if task.prompt_token_ids is not None
            else None,
            output_tokens=target,
        )


class OpenAILoadClient:
    """Own one HTTP client and shared generation measurements for a workload point."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        max_connections: int | None,
    ) -> None:
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
        self._completions_url = service.api_root.rstrip("/") + "/completions"
        self._request_url = (
            service.api_root.rstrip("/") + "/chat/completions"
            if service.chat_completions_url.rstrip("/") == self._completions_url
            else service.chat_completions_url
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.close()

    async def send(self, request: PreparedRequest) -> dict[str, Any]:
        """Send a prepared request and measure its timing, usage, and protocol failures."""
        request_fields = request.body
        completion = request.input_tokens is not None
        stream = request_fields["stream"]
        target_length = request.output_tokens
        model = request_fields["model"]
        started_at = time.perf_counter()
        timing = ChatStreamTiming()
        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_input_tokens: int | None = None
        generated_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        status_code: int | None = None
        error_message: str | None = None
        success = True
        try:
            response = await self._client.post(
                self._completions_url if completion else self._request_url,
                body=request_fields,
                cast_to=Completion if completion else ChatCompletion,
                stream=stream,
                stream_cls=AsyncStream[Completion]
                if completion
                else AsyncStream[ChatCompletionChunk],
            )
            status_code = httpx.codes.OK
            if stream:
                async for chunk in response:
                    received_at = time.perf_counter()
                    payload = chunk.model_dump(exclude_none=True)
                    timing.observe(payload, received_at)
                    if chunk.choices:
                        if completion:
                            generated_parts.append(chunk.choices[0].text)
                        else:
                            delta = chunk.choices[0].delta
                            if delta.content:
                                generated_parts.append(delta.content)
                            if delta.tool_calls:
                                tool_calls.extend(
                                    call.model_dump(exclude_none=True)
                                    for call in delta.tool_calls
                                )
                    if chunk.usage is not None:
                        input_tokens = int(chunk.usage.prompt_tokens)
                        output_tokens = int(chunk.usage.completion_tokens)
                        details = chunk.usage.prompt_tokens_details
                        if details is not None and details.cached_tokens is not None:
                            cached_input_tokens = int(details.cached_tokens)
            else:
                if response.choices:
                    if completion:
                        generated_parts.append(response.choices[0].text)
                    else:
                        message = response.choices[0].message
                        if message.content:
                            generated_parts.append(message.content)
                        if message.tool_calls:
                            tool_calls.extend(
                                call.model_dump(exclude_none=True)
                                for call in message.tool_calls
                            )
                if response.usage is not None:
                    input_tokens = int(response.usage.prompt_tokens)
                    output_tokens = int(response.usage.completion_tokens)
                    details = response.usage.prompt_tokens_details
                    if details is not None and details.cached_tokens is not None:
                        cached_input_tokens = int(details.cached_tokens)
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
        if success and completion and input_tokens != request.input_tokens:
            success = False
            error_message = f"Input length mismatch: sent {request.input_tokens} token IDs, service reported {input_tokens}"
        completed_at = (
            timing.last_output_at
            if stream and success and timing.last_output_at is not None
            else time.perf_counter()
        )
        latency = completed_at - started_at
        ttft = (
            timing.first_output_at - started_at
            if stream and timing.first_output_at is not None
            else None
        )
        return {
            "success": success,
            "started_at": started_at,
            "status_code": status_code,
            "stream": stream,
            "latency": latency,
            "ttft": ttft,
            "tpot": compute_tpot(latency, ttft, output_tokens),
            "inter_token_latencies": timing.intervals if stream else [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "target_output_tokens": target_length,
            "model": model,
            "cached_input_tokens": cached_input_tokens,
            "generated_text": "".join(generated_parts),
            "tool_calls": tool_calls,
            "error": error_message,
        }
