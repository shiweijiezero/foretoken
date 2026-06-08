"""评测指标(纯统计,无第三方依赖,可单测):延迟分位 / goodput 阶梯 / 原始吞吐 / 汇总。"""

from __future__ import annotations

from collections.abc import Sequence

# 默认 SLO 阶梯 (TTFT_ms, TPOT_ms):严 / 中 / 松——让饱和程度一眼可见。可被 replay --slo 覆盖。
DEFAULT_SLO: list[tuple[int, int]] = [(2000, 80), (10000, 150), (60000, 200)]


def parse_slo(specs: list[str]) -> list[tuple[int, int]]:
    """解析 --slo:["2000:80", "10000:150"] → [(2000,80),(10000,150)];空 → DEFAULT_SLO。"""
    out = [tuple(int(x) for x in s.split(":", 1)) for s in specs]
    return out or list(DEFAULT_SLO)  # type: ignore[return-value]


def fmt_ms(ms: float) -> str:
    """ms 人读:>=1000 用 s,否则 ms。"""
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def percentiles(results) -> dict:
    """TTFT / TPOT 的 p50/p90/p99(TPOT 仅统计 >0 的轮,单 token 轮无 TPOT)。"""
    ok = [r for r in results if r.ok]
    ttft = [r.ttft_ms for r in ok]
    tpot = [r.tpot_ms for r in ok if r.tpot_ms > 0]
    return {
        "ttft_ms": {f"p{int(q * 100)}": _pct(ttft, q) for q in (0.5, 0.9, 0.99)},
        "tpot_ms": {f"p{int(q * 100)}": _pct(tpot, q) for q in (0.5, 0.9, 0.99)},
    }


def goodput_ladder(
    results,
    *,
    duration_s: float,
    gpu_bytes: float,
    num_gpus: int,
    slo: Sequence[tuple[int, int]] = DEFAULT_SLO,
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
    """原始吞吐(不按 SLO 过滤):ok 轮输出 tok/s(及 /GPU)+ 完成请求 req/s。与 goodput 对照。"""
    ok = [r for r in results if r.ok]
    out_tok = sum(r.output_tokens for r in ok)
    return {
        "output_tok_s": out_tok / duration_s if duration_s > 0 else 0.0,
        "output_tok_s_per_gpu": (out_tok / duration_s / num_gpus)
        if duration_s > 0 and num_gpus > 0
        else 0.0,
        "request_s": len(ok) / duration_s if duration_s > 0 else 0.0,
    }


def summarize(
    results,
    *,
    duration_s: float,
    gpu_bytes: float,
    num_gpus: int,
    slo: Sequence[tuple[int, int]] = DEFAULT_SLO,
) -> dict:
    """聚合一次 run 的结果指标(延迟分位 + 完成数 + 原始吞吐 + goodput 阶梯)。"""
    return {
        "completed": sum(1 for r in results if r.ok),
        "total": len(results),
        "latency": percentiles(results),
        "throughput": throughput(results, duration_s=duration_s, num_gpus=num_gpus),
        "goodput": goodput_ladder(
            results, duration_s=duration_s, gpu_bytes=gpu_bytes, num_gpus=num_gpus, slo=slo
        ),
    }
