# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""闭环回放主循环(后端无关):跨会话并发、会话内串行,现场回复接回历史 + 混合时间调度。

附带两个轻量观测:进度(有 deadline 按时间、否则按请求)、客户端健康(事件循环延迟探针)。
"""

from __future__ import annotations

import asyncio
import sys
import time

from tqdm import tqdm

from foretoken.bench.core.types import TurnResult
from foretoken.bench.core.workload import next_send_ms


def _health(lags: list[float]) -> dict:
    """事件循环延迟统计 ms(漂移大 = 单事件循环饱和 = 客户端可能是瓶颈,非引擎)。"""
    if not lags:
        return {"loop_lag_mean_ms": 0.0, "loop_lag_max_ms": 0.0, "loop_lag_p99_ms": 0.0, "samples": 0}
    s = sorted(lags)
    return {
        "loop_lag_mean_ms": sum(s) / len(s),
        "loop_lag_max_ms": s[-1],
        "loop_lag_p99_ms": s[min(len(s) - 1, int(0.99 * len(s)))],
        "samples": len(s),
    }


def _progress_line(elapsed_s: float, deadline_s: float | None, total: int, c: dict) -> str:
    """进度行:有 deadline 按时间填、否则按完成请求填;后缀挂发出/完成/运行中计数。"""
    if deadline_s:
        frac = min(1.0, elapsed_s / deadline_s) if deadline_s > 0 else 0.0
        axis = f"{elapsed_s:.0f}/{deadline_s:.0f}s"
    else:
        frac = (c["done"] / total) if total else 0.0
        axis = f"{c['done']}/{total} 轮"
    n = int(frac * 20)
    bar = "█" * n + "░" * (20 - n)
    return (
        f"[{bar}] {frac * 100:3.0f}% {axis} · 发 {c['sent']}/{total}"
        f" · 完成 {c['done']} · 运行中 {c['sent'] - c['done']}"
    )


async def replay(
    sessions: dict[int, list[dict]],
    backend,
    *,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
) -> tuple[list[TurnResult], int, dict]:
    """闭环回放,返回 (已完成轮, 取消会话数, 客户端健康)。

    backend.gen_once(messages, request_id) → (text, ttft_ms, tpot_ms, e2e_ms, n, prompt_tokens, ok)
    (进程内引擎或 HTTP server 两种实现见 core.vllm_engine / core.backend)。
    deadline_s:墙钟上限(见 workload.deadline_seconds);到点取消运行中会话,只保留已完成轮。
    """
    t0 = min(s[0]["timestamp_ms"] for s in sessions.values())
    results: list[TurnResult] = []
    start = time.perf_counter()
    total = sum(len(s) for s in sessions.values())
    c = {"sent": 0, "done": 0}  # 发出 / 完成计数(运行中 = 发出 - 完成)

    async def run_session(turns: list[dict]) -> None:
        sid = turns[0]["session_id"]
        system = turns[0].get("system")
        messages = [{"role": "system", "content": system}] if system else []
        complete_prev = 0.0
        t_prev = turns[0]["timestamp_ms"]
        for k, turn in enumerate(turns):
            send_rel = next_send_ms(
                k, turn["timestamp_ms"], t_prev, complete_prev, t0, sec_multiplier
            )  # 已含 sec_multiplier 缩放(墙钟 ms),sleep 不再二次缩放
            now_ms = (time.perf_counter() - start) * 1000.0
            await asyncio.sleep(max(0.0, (send_rel - now_ms) / 1000.0))
            messages.append({"role": "user", "content": turn["user"]})
            send_ms = (time.perf_counter() - start) * 1000.0  # 实际发出时刻(经过 sleep 后)
            c["sent"] += 1
            text, ttft, tpot, e2e, n, n_prompt, ok = await backend.gen_once(messages, f"{sid}-{k}")
            c["done"] += 1
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

    lags: list[float] = []

    async def _lag_probe(interval: float = 0.5) -> None:
        """固定间隔 sleep,记实际 - 预期漂移 ms(事件循环饱和则漂移变大)。"""
        while True:
            t = time.perf_counter()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            lags.append((time.perf_counter() - t - interval) * 1000.0)

    tty = sys.stderr.isatty()
    units = round(deadline_s) if deadline_s else total  # 进度主轴:有 deadline 按时间、否则按请求
    pbar = tqdm(  # 交互(TTY)出真进度条;非 TTY(nohup 日志)禁用、改稀疏打行
        total=max(1, units), desc="回放", unit="s" if deadline_s else "轮",
        file=sys.stderr, disable=not tty, dynamic_ncols=True,
    )

    async def _progress() -> None:
        while True:
            try:
                await asyncio.sleep(1.0 if tty else 15.0)
            except asyncio.CancelledError:
                break
            el = time.perf_counter() - start
            if tty:  # tqdm:时间(或请求)填进度,请求计数作后缀
                pbar.update(min(units, round(el) if deadline_s else c["done"]) - pbar.n)
                pbar.set_postfix_str(f"发{c['sent']}/{total} 完成{c['done']} 运行中{c['sent'] - c['done']}")
            else:
                print(_progress_line(el, deadline_s, total, c), file=sys.stderr, flush=True)

    tasks = [asyncio.create_task(run_session(s)) for s in sessions.values()]
    if not tasks:
        pbar.close()
        return results, 0, _health(lags)
    aux = [asyncio.create_task(_lag_probe()), asyncio.create_task(_progress())]
    _done, pending = await asyncio.wait(tasks, timeout=deadline_s)
    cancelled = len(pending)
    if pending:  # 到点未完成的会话:取消并回收(只保留已记录的完成轮)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # 等取消落定
    for a in aux:  # 停观测任务
        a.cancel()
    await asyncio.gather(*aux, return_exceptions=True)
    if tty:
        pbar.update(units - pbar.n)  # 补满
        pbar.close()
    else:
        print(_progress_line(time.perf_counter() - start, deadline_s, total, c), file=sys.stderr, flush=True)
    if pending:
        print(f"达回放时限 {deadline_s:.0f}s:取消 {cancelled} 个未完成会话")
    return results, cancelled, _health(lags)
