# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Measure chat and tokenized text generation through one OpenAI-compatible client."""

from __future__ import annotations

import asyncio
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
from benchmarks.results.responses import ResponseContent, ResponseWriter


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
        response_writer: ResponseWriter | None = None,
    ) -> None:
        self.response_writer = response_writer
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

    async def send(
        self,
        request: PreparedRequest,
        *,
        request_id: str,
        phase: str,
        source_id: str,
        conversation_id: str | None,
        turn: int | None,
        retain_history: bool = False,
    ) -> dict[str, Any]:
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
        content = ResponseContent(
            raw=self.response_writer is not None, history=retain_history
        )
        transport_completed = False
        interrupted = False
        error_body = None
        status_code: int | None = None
        error_message: str | None = None
        success = True
        response = None
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
                    content.observe(payload, streaming=True, completion=completion)
            else:
                content.observe(
                    response.model_dump(exclude_none=False),
                    streaming=False,
                    completion=completion,
                )
            transport_completed = True
        except (APIError, httpx.HTTPError) as exc:
            success = False
            status_code = getattr(exc, "status_code", None)
            error_message = str(exc)
            error_body = exc.body if isinstance(exc, APIError) else None
        except asyncio.CancelledError:
            interrupted = True
            error_message = "Request interrupted"
            raise
        finally:
            transport_ended_at = time.perf_counter()
            if stream and response is not None:
                await response.close()
            if self.response_writer is not None:
                self.response_writer.write(
                    phase,
                    {
                        "request_id": request_id,
                        "phase": phase,
                        "source_id": source_id,
                        "conversation_id": conversation_id,
                        "turn": turn,
                        "request": request_fields,
                        "response": content.record(),
                        "transport_completed": transport_completed,
                        "interrupted": interrupted,
                        "status_code": status_code,
                        "error": error_body or error_message,
                    },
                )
        usage = content.usage
        if usage is not None:
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            cached_input_tokens = (usage.get("prompt_tokens_details") or {}).get(
                "cached_tokens"
            )

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
            else transport_ended_at
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
            "itl": timing.itl,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "target_output_tokens": target_length,
            "model": model,
            "cached_input_tokens": cached_input_tokens,
            "generated_text": "".join(content.generated),
            "tool_calls": content.tool_calls,
            "request_id": request_id,
            "phase": phase,
            "source_id": source_id,
            "finish_reason": content.finish_reason,
            "response_id": content.response_id,
            "response_model": content.response_model,
            "content_characters": content.content_characters,
            "reasoning_characters": content.reasoning_characters,
            "transport_completed": transport_completed,
            "error": error_message,
        }
