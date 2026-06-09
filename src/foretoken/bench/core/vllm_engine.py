# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""进程内 vLLM 引擎后端:InProcessBackend(流式生成)+ run_replay(起停引擎 + 逐 iteration 监控)。"""

from __future__ import annotations

import asyncio
import time

from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.metrics.loggers import StatLoggerBase

from foretoken.bench.core.loop import replay
from foretoken.bench.utils.timer import StreamTimer


class InProcessBackend:
    """进程内 AsyncLLM 后端:apply_chat_template → engine.generate 流式;被取消时 abort 释放槽位。"""

    def __init__(self, engine, tokenizer, sampling_params) -> None:
        self.engine = engine
        self.tokenizer = tokenizer
        self.sampling_params = sampling_params

    async def gen_once(self, messages: list[dict], request_id: str):
        prompt = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        timer = StreamTimer()
        n = n_prompt = 0
        text = ""
        try:
            async for out in self.engine.generate(prompt, self.sampling_params, request_id):
                co = out.outputs[0]
                if co.token_ids:
                    timer.mark_first()  # 首个含 token 的产出处打 TTFT(幂等)
                n = len(co.token_ids)
                n_prompt = len(out.prompt_token_ids or [])
                text = co.text
        except asyncio.CancelledError:
            await self.engine.abort(request_id)  # 取消:回收引擎里运行中的请求
            raise
        except Exception:  # noqa: BLE001  单条失败不中断整轮回放
            return "", 0.0, 0.0, 0.0, 0, 0, False
        if timer.first is None:  # 始终未出 token → 记失败
            return "", 0.0, 0.0, 0.0, 0, 0, False
        ttft, tpot, e2e = timer.metrics(n)
        return text, ttft, tpot, e2e, n, n_prompt, True


async def run_replay(
    sessions: dict[int, list[dict]],
    *,
    sampling: dict,
    engine_kwargs: dict,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
):
    """进程内起引擎 → 回放 → finally 关停。返回 (results, cancelled, duration_s, engine_stats, client_health)。

    engine_stats:逐 iteration 的引擎 SchedulerStats 序列(KV%/并发/排队),经自定义 stat logger 采集。
    """
    samples: list[tuple] = []

    class _Capture(StatLoggerBase):  # 每步收 SchedulerStats:KV 利用率 / 运行中 / 排队
        def __init__(self, *a, **k):
            pass

        def record(self, scheduler_stats, iteration_stats=None, mm_cache_stats=None, engine_idx=0):
            s = scheduler_stats
            if s is not None:
                samples.append((
                    time.perf_counter(),
                    getattr(s, "kv_cache_usage", 0.0),
                    getattr(s, "num_running_reqs", 0),
                    getattr(s, "num_waiting_reqs", 0),
                ))

        def log(self):
            pass

        def log_engine_initialized(self):
            pass

        def record_sleep_state(self, *a, **k):
            pass

    engine = AsyncLLM.from_engine_args(
        AsyncEngineArgs(**dict(engine_kwargs, disable_log_stats=False)),
        stat_loggers=[lambda *a, **k: _Capture()],
    )
    try:
        backend = InProcessBackend(engine, engine.get_tokenizer(), SamplingParams(**sampling))
        t = time.perf_counter()
        results, cancelled, health = await replay(
            sessions, backend, sec_multiplier=sec_multiplier, deadline_s=deadline_s
        )
        dur = time.perf_counter() - t
        stats = [  # 取回放期间(t 之后)的样本,时刻相对回放起点
            {"t": at - t, "kv": kv, "running": run, "waiting": wait}
            for (at, kv, run, wait) in samples
            if at - t >= 0
        ]
        return results, cancelled, dur, stats, health
    finally:
        engine.shutdown()  # 关停引擎、释放 GPU(正常 / 报错 / 中断都走)
