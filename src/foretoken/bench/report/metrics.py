# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""评测指标(纯统计):延迟分位 / goodput 阶梯 / 原始吞吐 / 汇总。"""

from __future__ import annotations

from collections.abc import Sequence


def parse_slo(specs: list[str]) -> list[tuple[int, int]]:
    """解析 SLO 阶梯:["2000:80", "10000:150"] → [(2000,80),(10000,150)];默认阶梯由 CLI 给。"""
    return [tuple(int(x) for x in s.split(":", 1)) for s in specs]  # type: ignore[misc]


def fmt_ms(ms: float) -> str:
    """ms 人读:>=1000 用 s,否则 ms。"""
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def percentiles(results) -> dict:
    """TTFT / TPOT / E2E 的 p50/p90/p99(TPOT 仅统计 >0 的轮,单 token 轮无 TPOT)。"""
    ok = [r for r in results if r.ok]
    cols = {
        "ttft_ms": [r.ttft_ms for r in ok],
        "tpot_ms": [r.tpot_ms for r in ok if r.tpot_ms > 0],
        "e2e_ms": [r.e2e_ms for r in ok],
    }
    return {k: {f"p{int(q * 100)}": _pct(v, q) for q in (0.5, 0.9, 0.99)} for k, v in cols.items()}


def goodput_ladder(
    results,
    *,
    duration_s: float,
    gpu_bytes: float,
    num_gpus: int,
    slo: Sequence[tuple[int, int]],
) -> list[dict]:
    """逐 SLO 档算:达成率 + 合格输出 tok/s(及 /GPU)+ 归一化 tok/(s·GPU字节)。

    达成 = ok 且 TTFT ≤ 档位 且 TPOT ≤ 档位;合格 token 只计达成轮的 output_tokens。
    """
    ok = [r for r in results if r.ok]
    rows: list[dict] = []
    for ttft_ms, tpot_ms in slo:
        good = [r for r in ok if r.ttft_ms <= ttft_ms and r.tpot_ms <= tpot_ms]
        good_tok = sum(r.output_tokens for r in good)
        rows.append(
            {
                "ttft_ms": ttft_ms,
                "tpot_ms": tpot_ms,
                "attain": (len(good) / len(ok)) if ok else 0.0,
                "good_tok_s": good_tok / duration_s if duration_s > 0 else 0.0,
                "good_tok_s_per_gpu": (good_tok / duration_s / num_gpus)
                if duration_s > 0 and num_gpus > 0
                else 0.0,
                "tok_per_s_gpubyte": (good_tok / (duration_s * gpu_bytes))
                if duration_s > 0 and gpu_bytes > 0
                else None,
            }
        )
    return rows


def throughput(results, *, duration_s: float, num_gpus: int) -> dict:
    """原始吞吐(不按 SLO 过滤):输出 / 输入 / 总 tok/s(及输出 /GPU)+ 完成请求 req/s。"""
    keys = ("output_tok_s", "output_tok_s_per_gpu", "prompt_tok_s", "total_tok_s", "request_s")
    if duration_s <= 0:
        return dict.fromkeys(keys, 0.0)
    ok = [r for r in results if r.ok]
    out_tok = sum(r.output_tokens for r in ok)
    prompt_tok = sum(getattr(r, "prompt_tokens", 0) for r in ok)
    g = num_gpus if num_gpus > 0 else 1
    return {
        "output_tok_s": out_tok / duration_s,
        "output_tok_s_per_gpu": out_tok / duration_s / g,
        "prompt_tok_s": prompt_tok / duration_s,
        "total_tok_s": (out_tok + prompt_tok) / duration_s,
        "request_s": len(ok) / duration_s,
    }


def engine_summary(engine_stats: list[dict] | None) -> dict | None:
    """引擎 SchedulerStats 时间序列 → 峰值 KV% / 均值 KV% / 最大在飞 / 最大排队。无则 None。"""
    if not engine_stats:
        return None
    kv = [s["kv"] for s in engine_stats]
    return {
        "peak_kv": max(kv),
        "mean_kv": sum(kv) / len(kv),
        "max_running": max(s["running"] for s in engine_stats),
        "max_waiting": max(s["waiting"] for s in engine_stats),
    }


def summarize(
    results,
    *,
    duration_s: float,
    gpu_bytes: float,
    num_gpus: int,
    slo: Sequence[tuple[int, int]],
    engine_stats: list[dict] | None = None,
) -> dict:
    """聚合 run 指标(延迟分位含 E2E + 尾部比 + 输出长度 + 吞吐 + goodput + 引擎 KV/并发)。"""
    ok = [r for r in results if r.ok]
    out_lens = [r.output_tokens for r in ok]
    lat = percentiles(results)
    p50 = lat["ttft_ms"]["p50"]
    return {
        "completed": len(ok),
        "total": len(results),
        "latency": lat,
        "tail_ratio_ttft": (lat["ttft_ms"]["p99"] / p50) if p50 else 0.0,
        "output_len": {
            "mean": (sum(out_lens) / len(out_lens)) if out_lens else 0.0,
            "total": sum(out_lens),
        },
        "throughput": throughput(results, duration_s=duration_s, num_gpus=num_gpus),
        "goodput": goodput_ladder(
            results, duration_s=duration_s, gpu_bytes=gpu_bytes, num_gpus=num_gpus, slo=slo
        ),
        "engine": engine_summary(engine_stats),
    }
