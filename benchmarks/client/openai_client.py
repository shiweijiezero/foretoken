# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""OpenAI-compatible chat client."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from openai import APIStatusError, AsyncOpenAI

from benchmarks.metrics.aggregator import compute_tpot


def _base_url(url: str) -> str:
    u = url.rstrip("/")
    return u.removesuffix("/chat/completions") or u


class OpenAICompatClient:
    """OpenAI-compatible chat client."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: int = 300,
        api_key: str = "",
    ):
        self.model = model
        self.client = AsyncOpenAI(
            base_url=_base_url(url),
            api_key=api_key or "EMPTY",
            max_retries=0,
            http_client=httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=10_000, max_keepalive_connections=10_000
                ),
            ),
        )

    async def start(self) -> None:
        """No-op; client is ready after ``__init__`` (kept for runner API)."""

    async def close(self) -> None:
        await self.client.close()

    async def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: int = 128,
        temperature: float = 0.0,
        stream: bool = True,
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
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            kwargs["stream_options"] = {"include_usage": True}

        t0 = time.perf_counter()
        ttft: Optional[float] = None
        in_tok = out_tok = n_content = 0
        status: Optional[int] = None
        try:
            resp = await self.client.chat.completions.create(**kwargs)
            status = 200
            if stream:
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
            elif resp.usage:
                in_tok = int(resp.usage.prompt_tokens or 0)
                out_tok = int(resp.usage.completion_tokens or 0)

            latency = time.perf_counter() - t0
            out_tok = out_tok or n_content
            return {
                "success": True,
                "status_code": status,
                "latency": latency,
                "ttft": ttft,
                "tpot": compute_tpot(latency, ttft, out_tok),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "error": None,
            }
        except Exception as e:
            latency = time.perf_counter() - t0
            out_tok = out_tok or n_content
            return {
                "success": False,
                "status_code": (
                    e.status_code if isinstance(e, APIStatusError) else status
                ),
                "latency": latency,
                "ttft": ttft,
                "tpot": compute_tpot(latency, ttft, out_tok),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "error": str(e)[:500],
            }
