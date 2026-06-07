"""闭环会话回放:按会话维护上下文、模型现场生成回复拼下一轮,采集 TTFT/TPOT/E2E、算 goodput。

时间调度(每会话):首轮按绝对 timestamp;之后上一轮按时完成(C ≤ T_k)则按绝对 T_k,超时则
C + 原间隔(T_k − T_{k-1})。跨会话并发、会话内串行。需 OpenAI 兼容 endpoint(vllm serve)。
纯函数(next_send_ms / group_sessions / parse_window / goodput_per_gpu_byte_second)可本地测;
httpx 延迟导入,未安装亦可测纯函数。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass
class TurnResult:
    session_id: int
    turn: int
    ttft_ms: float
    tpot_ms: float
    e2e_ms: float
    output_tokens: int
    text: str = ""  # 现场生成的回复(供无损校验 / 对照)
    ok: bool = True


def next_send_ms(turn_idx: int, t_cur: int, t_prev: int, complete_prev_ms: float, t0: int) -> float:
    """该轮发出的相对(对 t0)时刻 ms。首轮 = 绝对;上一轮按时完成 → 绝对,超时 → 完成 + 原间隔。"""
    t_cur_rel = t_cur - t0
    if turn_idx == 0 or complete_prev_ms <= t_cur_rel:
        return float(t_cur_rel)
    return complete_prev_ms + (t_cur - t_prev)  # 超时:完成时刻 + 原本的思考间隔


def group_sessions(
    rows: Iterable[dict], *, window: tuple[int, int] | None = None
) -> dict[int, list[dict]]:
    """按 session_id 分组(组内按 turn 排序);window=(a_ms,b_ms) 则仅纳入首轮 timestamp 落窗内的会话。

    准入按会话首轮、整组纳入——背压把会话拖出窗也跑完,不硬截。
    """
    sess: dict[int, list[dict]] = {}
    for r in rows:
        sess.setdefault(r["session_id"], []).append(r)
    for s in sess.values():
        s.sort(key=lambda r: r["turn"])
    if window is not None:
        t0 = min(s[0]["timestamp_ms"] for s in sess.values())
        a, b = window
        sess = {k: s for k, s in sess.items() if a <= s[0]["timestamp_ms"] - t0 <= b}
    return sess


def parse_window(spec: str | None) -> tuple[int, int] | None:
    """'N' → (0, N 分);'A:B' → (A 分, B 分);None → 全量。单位 ms。"""
    if not spec:
        return None
    if ":" in spec:
        a, b = spec.split(":", 1)
        return (int(float(a) * 60_000), int(float(b) * 60_000))
    return (0, int(float(spec) * 60_000))


def goodput_per_gpu_byte_second(
    results: Iterable[TurnResult],
    *,
    ttft_ms: float,
    tpot_ms: float,
    duration_s: float,
    gpu_bytes: float,
) -> float:
    """满足 SLO(TTFT 且 TPOT)的有效 output token / (时长 × GPU 字节)——统一标尺。"""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if gpu_bytes <= 0:
        raise ValueError("gpu_bytes must be > 0")
    good = sum(
        r.output_tokens for r in results if r.ok and r.ttft_ms <= ttft_ms and r.tpot_ms <= tpot_ms
    )
    return good / (duration_s * gpu_bytes)


async def _chat_once(client, base_url, model, messages, sampling):
    """流式 chat completion → (text, ttft_ms, tpot_ms, e2e_ms, n_tokens, ok)。需 httpx。

    sampling:采样参数(temperature/top_p/top_k/max_tokens/seed),直接并入请求(vLLM 接受 top_k/seed)。
    """
    start = time.perf_counter()
    first: float | None = None
    chunks: list[str] = []
    payload = {"model": model, "messages": messages, "stream": True, **sampling}
    try:
        async with client.stream("POST", f"{base_url}/v1/chat/completions", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line.strip().endswith("[DONE]"):
                    continue
                try:
                    delta = json.loads(line[6:])["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, ValueError):
                    delta = None
                if delta:
                    if first is None:
                        first = time.perf_counter()
                    chunks.append(delta)
    except Exception:  # noqa: BLE001  单条失败不中断整轮回放
        return "", 0.0, 0.0, 0.0, 0, False
    if first is None:
        return "", 0.0, 0.0, 0.0, 0, False
    end = time.perf_counter()
    n = len(chunks)
    ttft = (first - start) * 1000.0
    tpot = ((end - first) * 1000.0 / (n - 1)) if n > 1 else 0.0
    return "".join(chunks), ttft, tpot, (end - start) * 1000.0, n, True


async def replay(
    sessions: dict[int, list[dict]],
    base_url: str,
    model: str,
    *,
    sampling: dict,
    sec_multiplier: float = 1.0,
) -> list[TurnResult]:
    """闭环回放:跨会话并发、会话内串行(现场生成回复 + 混合时间调度)。需 httpx。

    sampling:官方采样参数 + seed(见 model_params / docs/14),逐请求一致以保可复现。
    """
    try:
        import httpx
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 httpx:pip install httpx(或装 foretoken[server])") from e
    t0 = min(s[0]["timestamp_ms"] for s in sessions.values())
    results: list[TurnResult] = []

    async with httpx.AsyncClient(timeout=None) as client:
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
                text, ttft, tpot, e2e, n, ok = await _chat_once(
                    client, base_url, model, messages, sampling
                )
                complete_prev = (time.perf_counter() - start) * 1000.0  # C_k 相对完成时刻
                messages.append({"role": "assistant", "content": text})  # 现场回复接回历史
                results.append(TurnResult(sid, k, ttft, tpot, e2e, n, text, ok))
                t_prev = turn["timestamp_ms"]

        await asyncio.gather(*(run_session(s) for s in sessions.values()))
    return results


def _summary(results: list[TurnResult]) -> None:
    """打印 TTFT/TPOT/E2E 分位 + 完成数(简版;详细汇总 / 图见展示脚本)。"""
    ok = [r for r in results if r.ok]

    def pct(xs: list[float], q: float) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0

    ttfts = [r.ttft_ms for r in ok]
    tpots = [r.tpot_ms for r in ok if r.tpot_ms > 0]
    print(f"请求 {len(results)}(ok {len(ok)})")
    if ttfts:
        d = f"p50={pct(ttfts, 0.5):.0f} p90={pct(ttfts, 0.9):.0f} p99={pct(ttfts, 0.99):.0f}"
        print(f"TTFT ms  {d}")
    if tpots:
        d = f"p50={pct(tpots, 0.5):.0f} p90={pct(tpots, 0.9):.0f} p99={pct(tpots, 0.99):.0f}"
        print(f"TPOT ms  {d}")


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="闭环回放 foretoken-trace,测 TTFT/TPOT/goodput")
    ap.add_argument("--dataset", default="weijiezz/foretoken-trace")
    ap.add_argument("--split", required=True, help="conversation / mooncake / toolagent")
    ap.add_argument("--window", default=None, help="时间窗(分钟):N 或 A:B")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--temperature", type=float, default=None, help="覆盖 config(默认按模型)")
    ap.add_argument("--top-p", type=float, default=None, help="覆盖 config")
    ap.add_argument("--top-k", type=int, default=None, help="覆盖 config")
    ap.add_argument("--max-tokens", type=int, default=None, help="覆盖 config")
    ap.add_argument("--seed", type=int, default=0, help="采样 seed(固定以可复现)")
    ap.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="透传任意 vLLM 采样参数(可重复),如 --param repetition_penalty=1.1",
    )
    args = ap.parse_args()

    try:
        from datasets import load_dataset
    except ImportError as e:
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e

    from foretoken.bench.model_params import params_for

    sampling = params_for(args.model)  # config/models/<model>.toml 的官方采样
    overrides = {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "max_tokens": args.max_tokens,
    }
    sampling.update({k: v for k, v in overrides.items() if v is not None})  # 常用项 CLI 覆盖
    for kv in args.param:  # 任意 vLLM 参数透传(K=V,值按 JSON 解析)
        k, _, v = kv.partition("=")
        try:
            sampling[k] = json.loads(v)
        except json.JSONDecodeError:
            sampling[k] = v
    sampling["seed"] = args.seed
    rows = list(load_dataset(args.dataset, args.split, split="train"))
    sessions = group_sessions(rows, window=parse_window(args.window))
    n_turns = sum(len(s) for s in sessions.values())
    print(f"{args.split}: {len(sessions)} 会话, {n_turns} 轮 | 采样 {sampling}")
    results = asyncio.run(replay(sessions, args.base_url, args.model, sampling=sampling))
    _summary(results)
