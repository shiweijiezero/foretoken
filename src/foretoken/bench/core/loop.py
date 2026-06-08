# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""闭环回放主循环(后端无关):跨会话并发、会话内串行,现场回复接回历史 + 混合时间调度。"""

from __future__ import annotations

import asyncio
import time

from foretoken.bench.core.types import TurnResult
from foretoken.bench.core.workload import next_send_ms


async def replay(
    sessions: dict[int, list[dict]],
    backend,
    *,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
) -> tuple[list[TurnResult], int]:
    """闭环回放,返回 (已完成轮, 取消会话数)。

    backend.gen_once(messages, request_id) → (text, ttft_ms, tpot_ms, e2e_ms, n, prompt_tokens, ok)
    (进程内引擎或 HTTP server 两种实现见 core.vllm_engine / core.backend)。
    deadline_s:墙钟上限(见 workload.deadline_seconds);到点取消运行中会话,只保留已完成轮。
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
            text, ttft, tpot, e2e, n, n_prompt, ok = await backend.gen_once(messages, f"{sid}-{k}")
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
