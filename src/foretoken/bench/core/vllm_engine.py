# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""进程内 vLLM 引擎执行层:流式生成、闭环回放循环、引擎起停与逐 iteration 监控。

回放循环 replay() 接收已建好的 engine/tokenizer(不直接触碰 vLLM 类),便于以 fake 引擎单测。
"""

from __future__ import annotations

import asyncio
import time

from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.metrics.loggers import StatLoggerBase

from foretoken.bench.core.timer import StreamTimer
from foretoken.bench.core.types import TurnResult
from foretoken.bench.core.workload import next_send_ms


async def _gen_once(engine, tokenizer, messages, sampling_params, request_id):
    """进程内流式生成 → (text, ttft_ms, tpot_ms, e2e_ms, n_tokens, prompt_tokens, ok)。

    apply_chat_template 把对话拼成 prompt;engine.generate 流式产出累积 RequestOutput,
    首个含 token 的产出计 TTFT,末次的 token 数计长度。被取消时 abort 该请求以释放引擎槽位。
    """
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    timer = StreamTimer()
    n = 0
    n_prompt = 0
    text = ""
    try:
        async for out in engine.generate(prompt, sampling_params, request_id):
            co = out.outputs[0]
            if co.token_ids:
                timer.mark_first()  # 首个含 token 的产出处打 TTFT(幂等)
            n = len(co.token_ids)
            n_prompt = len(out.prompt_token_ids or [])
            text = co.text
    except asyncio.CancelledError:
        await engine.abort(request_id)  # 取消:回收引擎中的在飞请求
        raise
    except Exception:  # noqa: BLE001  单条失败不中断整轮回放
        return "", 0.0, 0.0, 0.0, 0, 0, False
    if timer.first is None:  # 始终未出 token → 记失败
        return "", 0.0, 0.0, 0.0, 0, 0, False
    ttft, tpot, e2e = timer.metrics(n)
    return text, ttft, tpot, e2e, n, n_prompt, True


async def replay(
    sessions: dict[int, list[dict]],
    engine,
    tokenizer,
    *,
    sampling_params,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
) -> tuple[list[TurnResult], int]:
    """闭环回放:跨会话并发、会话内串行(现场生成回复 + 混合时间调度)。返回 (已完成轮, 取消会话数)。

    engine/tokenizer:进程内 vLLM 引擎与其分词器(见 run_replay)。sampling_params 逐请求一致以可复现。
    deadline_s:墙钟上限(见 workload.deadline_seconds);到点取消在飞会话,只保留已完成轮。
    """
    t0 = min(s[0]["timestamp_ms"] for s in sessions.values())
    results: list[TurnResult] = []
    start = time.perf_counter()

    async def run_session(turns: list[dict]) -> None:
        sid = turns[0]["session_id"]
        system = turns[0].get("system")
        messages = [{"role": "system", "content": system}] if system else []
        complete_prev = 0.0
        t_prev = turns[0]["timestamp_ms"]
        for k, turn in enumerate(turns):
            send_rel = next_send_ms(k, turn["timestamp_ms"], t_prev, complete_prev, t0)
            now_ms = (time.perf_counter() - start) * 1000.0
            await asyncio.sleep(max(0.0, (send_rel * sec_multiplier - now_ms) / 1000.0))
            messages.append({"role": "user", "content": turn["user"]})
            send_ms = (time.perf_counter() - start) * 1000.0  # 实际发出时刻(经过 sleep 后)
            text, ttft, tpot, e2e, n, n_prompt, ok = await _gen_once(
                engine, tokenizer, messages, sampling_params, f"{sid}-{k}"
            )
            complete_prev = (time.perf_counter() - start) * 1000.0  # C_k 相对完成时刻
            messages.append({"role": "assistant", "content": text})  # 现场回复接回历史
            results.append(
                TurnResult(
                    sid, k, ttft, tpot, e2e, n, prompt_tokens=n_prompt,
                    send_ms=send_ms, complete_ms=complete_prev, text=text,
                    user=turn["user"], system=system if k == 0 else None, ok=ok,
                )
            )
            t_prev = turn["timestamp_ms"]

    tasks = [asyncio.create_task(run_session(s)) for s in sessions.values()]
    if not tasks:
        return results, 0
    _done, pending = await asyncio.wait(tasks, timeout=deadline_s)
    cancelled = len(pending)
    if pending:  # 到点未完成的会话:取消并回收(只保留已记录的完成轮)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # 等取消落定
        print(f"达回放时限 {deadline_s:.0f}s:取消 {cancelled} 个未完成会话")
    return results, cancelled


async def run_replay(
    sessions: dict[int, list[dict]],
    *,
    sampling: dict,
    engine_kwargs: dict,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
):
    """进程内起引擎 → 回放 → finally 关停。返回 (results, cancelled, duration_s, engine_stats)。

    engine_stats:逐 iteration 的引擎 SchedulerStats 序列(KV%/并发/排队),经自定义 stat logger 采集。
    """
    samples: list[tuple] = []

    class _Capture(StatLoggerBase):  # 每步收 SchedulerStats:KV 利用率 / 在飞 / 排队
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
        tokenizer = engine.get_tokenizer()
        sampling_params = SamplingParams(**sampling)
        t = time.perf_counter()
        results, cancelled = await replay(
            sessions,
            engine,
            tokenizer,
            sampling_params=sampling_params,
            sec_multiplier=sec_multiplier,
            deadline_s=deadline_s,
        )
        dur = time.perf_counter() - t
        stats = [  # 取回放期间(t 之后)的样本,时刻相对回放起点
            {"t": at - t, "kv": kv, "running": run, "waiting": wait}
            for (at, kv, run, wait) in samples
            if at - t >= 0
        ]
        return results, cancelled, dur, stats
    finally:
        engine.shutdown()  # 关停引擎、释放 GPU(正常 / 报错 / 中断都走)
