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
    """进程内起引擎 → 回放 → finally 关停。
    返回 (results, cancelled, duration_s, engine_stats, engine_requests, client_health)。

    engine_stats:逐 iteration 快照(KV%/并发/排队 + prefix 命中增量 + 抢占 + decode token)。
    engine_requests:每个完成请求的引擎分解(排队 / prefill / decode 时间、TPOT、finish_reason、缓存命中)。
    """
    samples: list[dict] = []
    fin: list[dict] = []

    class _Capture(StatLoggerBase):  # 逐 iteration 快照 + 完成请求分解
        def __init__(self, *a, **k):
            pass

        def record(self, scheduler_stats, iteration_stats=None, mm_cache_stats=None, engine_idx=0):
            s, it = scheduler_stats, iteration_stats
            if s is not None:  # 调度器快照 + prefix / decode / 抢占增量
                pc = getattr(s, "prefix_cache_stats", None)
                samples.append({
                    "at": time.perf_counter(),
                    "kv": getattr(s, "kv_cache_usage", 0.0),
                    "running": getattr(s, "num_running_reqs", 0),
                    "waiting": getattr(s, "num_waiting_reqs", 0),
                    "pc_q": getattr(pc, "queries", 0) if pc is not None else 0,
                    "pc_h": getattr(pc, "hits", 0) if pc is not None else 0,
                    "gen_tok": getattr(it, "num_generation_tokens", 0) if it is not None else 0,
                    "preempted": getattr(it, "num_preempted_reqs", 0) if it is not None else 0,
                })
            if it is not None:  # 完成请求的时间分解
                for fr in getattr(it, "finished_requests", None) or []:
                    fin.append({
                        "finish_reason": str(getattr(fr, "finish_reason", "")),
                        "e2e_s": getattr(fr, "e2e_latency", 0.0),
                        "queued_s": getattr(fr, "queued_time", 0.0),
                        "prefill_s": getattr(fr, "prefill_time", 0.0),
                        "decode_s": getattr(fr, "decode_time", 0.0),
                        "mtpot_s": getattr(fr, "mean_time_per_output_token", 0.0),
                        "n_prompt": getattr(fr, "num_prompt_tokens", 0),
                        "n_gen": getattr(fr, "num_generation_tokens", 0),
                        "cached": getattr(fr, "num_cached_tokens", 0),
                    })

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
        stats = [  # 回放期间样本,时刻相对回放起点
            {k: v for k, v in s.items() if k != "at"} | {"t": s["at"] - t}
            for s in samples
            if s["at"] - t >= 0
        ]
        return results, cancelled, dur, stats, fin, health
    finally:
        engine.shutdown()  # 关停引擎、释放 GPU(正常 / 报错 / 中断都走)
