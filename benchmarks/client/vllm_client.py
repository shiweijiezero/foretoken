from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiohttp

from benchmarks.metrics.aggregator import compute_tpot


def _result(
    *,
    success: bool,
    status_code: Optional[int],
    latency: float,
    ttft: Optional[float] = None,
    tpot: Optional[float] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "status_code": status_code,
        "latency": latency,
        "ttft": ttft,
        "tpot": tpot,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error": error,
    }


class VLLMClient:
    """OpenAI-compatible chat client with optional streaming (TTFT)."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: int = 300,
        api_key: str = "",
    ):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self.session is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=300),
                timeout=self.timeout,
                headers=headers,
            )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: int = 128,
        temperature: float = 0.0,
        stream: bool = True,
    ) -> dict[str, Any]:
        """
        Single request.

        Returns keys:
          success, status_code, latency, ttft, tpot,
          input_tokens, output_tokens, error
        """
        if self.session is None:
            await self.start()

        chat_messages = self._build_messages(prompt, messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream_options"] = {"include_usage": True}
            return await self._generate_stream(payload)
        return await self._generate_non_stream(payload)

    def _build_messages(
        self,
        prompt: Optional[str],
        messages: Optional[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        if messages is not None:
            return messages
        if prompt is not None:
            return [{"role": "user", "content": prompt}]
        raise ValueError("Either prompt or messages must be provided")

    async def _generate_non_stream(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        start_time = time.perf_counter()
        try:
            assert self.session is not None
            async with self.session.post(self.url, json=payload) as response:
                data = await response.json()
                status = response.status

            latency = time.perf_counter() - start_time
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            in_tok = int(usage.get("prompt_tokens", 0) or 0)
            out_tok = int(usage.get("completion_tokens", 0) or 0)
            ok = status < 400
            return _result(
                success=ok,
                status_code=status,
                latency=latency,
                input_tokens=in_tok,
                output_tokens=out_tok,
                error=None if ok else str(data),
            )
        except Exception as e:
            return _result(
                success=False,
                status_code=None,
                latency=time.perf_counter() - start_time,
                error=str(e),
            )

    async def _generate_stream(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        start_time = time.perf_counter()
        ttft: Optional[float] = None
        output_tokens = 0
        input_tokens = 0
        content_tokens = 0
        status_code: Optional[int] = None

        try:
            assert self.session is not None
            async with self.session.post(self.url, json=payload) as response:
                status_code = response.status
                if status_code >= 400:
                    body = await response.text()
                    return _result(
                        success=False,
                        status_code=status_code,
                        latency=time.perf_counter() - start_time,
                        error=body[:500],
                    )

                async for raw in response.content:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    usage = chunk.get("usage")
                    if usage:
                        input_tokens = int(
                            usage.get("prompt_tokens", input_tokens)
                            or input_tokens
                        )
                        output_tokens = int(
                            usage.get("completion_tokens", output_tokens)
                            or output_tokens
                        )

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content or delta.get("tool_calls"):
                        if ttft is None:
                            ttft = time.perf_counter() - start_time
                        if content:
                            content_tokens += 1

            latency = time.perf_counter() - start_time
            if output_tokens <= 0:
                output_tokens = content_tokens
            return _result(
                success=True,
                status_code=status_code,
                latency=latency,
                ttft=ttft,
                tpot=compute_tpot(latency, ttft, output_tokens),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception as e:
            latency = time.perf_counter() - start_time
            return _result(
                success=False,
                status_code=status_code,
                latency=latency,
                ttft=ttft,
                tpot=compute_tpot(latency, ttft, output_tokens),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=str(e),
            )
