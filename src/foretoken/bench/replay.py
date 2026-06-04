"""B 路径回放器:按真实 timestamp 异步回放缝合 trace(stitch 模式 B 产物),采 TTFT/TPOT,算 goodput。

⚠️ 代价说明:vLLM 原生 `timed_trace` 只认 hash、`custom` 没 timestamp —— **都吃不了"真实 prompt
+ 真实到达"**。所以"含多用户并发的一套全真"必须自写回放,这等于把 vLLM bench serve 的
发请求 / 采集 / goodput **最小重造**(B 路径下必须复活)。真发请求需 OpenAI 兼容 endpoint(vllm serve)。

纯函数(relative_delays_ms / goodput_per_gpu_byte_second)本地可测;httpx 延迟 import,不装也能测。
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass
class ReplayResult:
    req_id: str
    ttft_ms: float
    tpot_ms: float
    output_tokens: int
    ok: bool = True


def relative_delays_ms(timestamps_ms: Sequence[float], sec_multiplier: float = 1.0) -> list[float]:
    """绝对 timestamp → 相对起跑延迟(第一条为 0)。保留真实间隔与并发(同刻 → 同延迟)。"""
    if not timestamps_ms:
        return []
    t0 = min(timestamps_ms)
    return [max(0.0, t - t0) * sec_multiplier for t in timestamps_ms]


def goodput_per_gpu_byte_second(
    results: Iterable[ReplayResult],
    *,
    ttft_ms: float,
    tpot_ms: float,
    duration_s: float,
    gpu_bytes: float,
) -> float:
    """满足 SLO(TTFT 且 TPOT)的有效 output token / (时长 × GPU 字节)—— 统一标尺。"""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if gpu_bytes <= 0:
        raise ValueError("gpu_bytes must be > 0")
    good = sum(
        r.output_tokens
        for r in results
        if r.ok and r.ttft_ms <= ttft_ms and r.tpot_ms <= tpot_ms
    )
    return good / (duration_s * gpu_bytes)


async def _send_one(client, base_url, model, prompt, max_tokens, req_id) -> ReplayResult:
    """流式发一条 completion,采 TTFT(首 token)+ TPOT(后续均摊)。需要 httpx。"""
    import time

    start = time.perf_counter()
    first: float | None = None
    n = 0
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0,
    }
    try:
        async with client.stream("POST", f"{base_url}/v1/completions", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line.strip().endswith("[DONE]"):
                    continue
                if first is None:
                    first = time.perf_counter()
                n += 1
        if first is None:
            return ReplayResult(req_id, 0.0, 0.0, 0, ok=False)
        ttft = (first - start) * 1000.0
        tpot = ((time.perf_counter() - first) * 1000.0 / (n - 1)) if n > 1 else 0.0
        return ReplayResult(req_id, ttft, tpot, n, ok=True)
    except Exception:  # noqa: BLE001  回放器要稳:单条失败不拖垮整轮
        return ReplayResult(req_id, 0.0, 0.0, 0, ok=False)


async def replay(
    requests: list[dict],
    base_url: str,
    model: str,
    *,
    sec_multiplier: float = 1.0,
) -> list[ReplayResult]:
    """按 `timestamp_ms` 异步回放(真实到达 + 真实并发),返回每条的 ReplayResult。需要 httpx。"""
    try:
        import httpx
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 httpx:pip install httpx(或装 foretoken[server])") from e

    delays = relative_delays_ms([r["timestamp_ms"] for r in requests], sec_multiplier)
    results: list[ReplayResult] = []

    async with httpx.AsyncClient(timeout=None) as client:

        async def _one(req: dict, delay_ms: float) -> None:
            await asyncio.sleep(delay_ms / 1000.0)
            results.append(
                await _send_one(
                    client,
                    base_url,
                    model,
                    req["prompt"],
                    int(req.get("expected_output_len", 128)),
                    str(req.get("session_or_id", req.get("hash_ids", ""))),
                )
            )

        await asyncio.gather(*(_one(r, d) for r, d in zip(requests, delays)))
    return results
