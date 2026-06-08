# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""回放后端接口 + HTTP 实现:打 OpenAI 兼容 `vllm serve` 的 `/v1/chat/completions`(流式)。

进程内引擎实现见 `core.vllm_engine.InProcessBackend`。两者同签名,`core.loop.replay` 不区分。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from openai import AsyncOpenAI

from foretoken.bench.core.timer import StreamTimer

# (text, ttft_ms, tpot_ms, e2e_ms, n_tokens, prompt_tokens, ok)
GenResult = tuple[str, float, float, float, int, int, bool]

# OpenAI 原生采样字段;其余(top_k / repetition_penalty / min_p 等 vLLM 扩展)走 extra_body
_NATIVE = {
    "temperature", "top_p", "max_tokens", "seed",
    "frequency_penalty", "presence_penalty", "stop",
}


class Backend(Protocol):
    async def gen_once(self, messages: list[dict], request_id: str) -> GenResult: ...


class HttpBackend:
    """打 `vllm serve` 的 `/v1/chat/completions`(stream=True);首 chunk 计 TTFT、间隔计 TPOT。"""

    def __init__(
        self, base_url: str, model: str, sampling: dict, *, api_key: str = "EMPTY"
    ) -> None:
        self.client = AsyncOpenAI(base_url=base_url.rstrip("/") + "/v1", api_key=api_key)
        self.model = model
        self.native = {k: v for k, v in sampling.items() if k in _NATIVE}
        self.extra = {k: v for k, v in sampling.items() if k not in _NATIVE}  # vLLM 扩展采样

    async def gen_once(self, messages: list[dict], request_id: str) -> GenResult:
        timer = StreamTimer()
        n = n_chunks = n_prompt = 0
        text = ""
        stream = None
        try:
            stream = await self.client.chat.completions.create(
                model=self.model, messages=messages, stream=True,
                stream_options={"include_usage": True}, extra_body=self.extra, **self.native,
            )
            async for chunk in stream:
                if chunk.usage:  # include_usage:末包给准确 token 数
                    n_prompt = chunk.usage.prompt_tokens
                    n = chunk.usage.completion_tokens
                for ch in chunk.choices:
                    piece = ch.delta.content if ch.delta else None
                    if piece:
                        timer.mark_first()
                        text += piece
                        n_chunks += 1
        except asyncio.CancelledError:
            if stream is not None:
                await stream.close()  # 断流 → server abort 该请求
            raise
        except Exception:  # noqa: BLE001  单条失败不中断整轮回放
            return "", 0.0, 0.0, 0.0, 0, 0, False
        if timer.first is None:
            return "", 0.0, 0.0, 0.0, 0, 0, False
        n = n or n_chunks  # usage 缺失时回退按 chunk 计
        ttft, tpot, e2e = timer.metrics(n)
        return text, ttft, tpot, e2e, n, n_prompt, True

    async def aclose(self) -> None:
        await self.client.close()
