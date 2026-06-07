"""实验记录:把一次回放结果写成人可读产物 + 机器可读原始,便于横向比与事后重算。

单 run 一目录:
  run.json     完整可复现配置 + 聚合指标(机器读)
  turns.jsonl  每轮一行原始(机器读,事后换 SLO 重算 / 画图)
  summary.md   人打开第一眼看的摘要(config / 完成 / 延迟 / goodput 阶梯)
  *.png        TTFT / TPOT 分布图(best-effort,无 matplotlib 则跳过)
跨 run:runs/INDEX.md 排行榜,append 一行 / run。

纯统计(percentiles / goodput_ladder / summarize)无第三方依赖,可单测。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

# 默认 SLO 阶梯 (TTFT_ms, TPOT_ms):严 / 中 / 松——让饱和程度一眼可见。
DEFAULT_SLO: list[tuple[int, int]] = [(2000, 80), (10000, 150), (60000, 200)]


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


def _fmt_ms(ms: float) -> str:
    """ms 人读:>=1000 用 s,否则 ms。"""
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _load_spec(w: dict) -> str:
    """负载下采样口径(人读):n=N / sample=F / full。"""
    if w.get("n_requests"):
        return f"n={w['n_requests']}"
    if w.get("sample"):
        return f"sample={w['sample']}"
    return "full"


def _render_md(run: dict) -> str:
    m, gpu, w = run["model"], run["gpu"], run["workload"]
    lat, lo, tp = run["latency"], run["load"], run["throughput"]
    off = lo.get("offered_turn_s")
    off_s = f" · offered {off:.2f} 轮/s" if off else ""
    load_spec = _load_spec(w)
    eng = m.get("engine_args", {})
    eng_s = " ".join(f"{k}={v}" for k, v in eng.items() if k != "model")
    done, total = run["completed"], run["total"]
    pct = f"{100 * done / total:.0f}%" if total else "0%"
    lines = [
        f"# {m['name']} · {run['tag']} · {run['workload']['split']} {run['workload']['window']}",
        f"{run['timestamp']} · {run['host']} · {gpu['count']}×{gpu['name']} · "
        f"vllm {run['vllm']} · commit {run['commit']}",
        "",
        f"config    {eng_s}",
        f"sampling  {' '.join(f'{k}={v}' for k, v in run['sampling'].items())}",
        f"workload  {w['split']} window {w['window']} {load_spec} "
        f"sec_mult={w['sec_multiplier']} deadline "
        f"{f'{dl:.0f}s' if (dl := w['deadline_s']) else 'off'}",
        "",
        f"load        {lo['sessions']} 会话 / {lo['turns']} 轮{off_s}        "
        f"completed {done} 轮 ({pct}) · cancelled {run['cancelled_sessions']}",
        f"duration    {run['duration_s']:.0f} s",
        "",
        "latency        p50       p90       p99",
        f"  TTFT       {_fmt_ms(lat['ttft_ms']['p50']):>8} {_fmt_ms(lat['ttft_ms']['p90']):>9} "
        f"{_fmt_ms(lat['ttft_ms']['p99']):>9}",
        f"  TPOT       {_fmt_ms(lat['tpot_ms']['p50']):>8} {_fmt_ms(lat['tpot_ms']['p90']):>9} "
        f"{_fmt_ms(lat['tpot_ms']['p99']):>9}",
        "",
        f"throughput   输出 {tp['output_tok_s']:.0f} tok/s ({tp['output_tok_s_per_gpu']:.0f}/GPU)"
        f" · 完成 {tp['request_s']:.2f} req/s",
        "",
        "goodput (SLO 达成阶梯;good=满足该档 SLO 的有效输出)",
        "  SLO(TTFT,TPOT)        达成%   good tok/s   /GPU   tok/(s·GPU字节)",
    ]
    for g in run["goodput"]:
        norm = "-" if g["tok_per_s_gpubyte"] is None else f"{g['tok_per_s_gpubyte']:.2e}"
        lines.append(
            f"  {_fmt_ms(g['ttft_ms']):>4}, {g['tpot_ms']:>4}ms        "
            f"{100 * g['attain']:>5.0f}%   {g['good_tok_s']:>9.0f}   "
            f"{g['good_tok_s_per_gpu']:>5.0f}   {norm:>14}"
        )
    return "\n".join(lines) + "\n"


def _plots(results, out: Path) -> list[str]:
    """TTFT / TPOT 累积分布图(CDF;重尾用 CDF 最清楚)。无 matplotlib 则跳过。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    ok = [r for r in results if r.ok]
    made: list[str] = []
    for key, fname, label in (
        ("ttft_ms", "ttft_cdf.png", "TTFT (ms)"),
        ("tpot_ms", "tpot_cdf.png", "TPOT (ms)"),
    ):
        xs = sorted(v for v in (getattr(r, key) for r in ok) if v > 0)
        if not xs:
            continue
        ys = [(i + 1) / len(xs) for i in range(len(xs))]
        fig, ax = plt.subplots(figsize=(5, 3.2))
        ax.plot(xs, ys)
        ax.set_xscale("log")
        ax.set_xlabel(label)
        ax.set_ylabel("CDF")
        ax.set_title(f"{label} CDF (n={len(xs)})")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out / fname, dpi=110)
        plt.close(fig)
        made.append(fname)
    return made


def write_run(
    results, meta: dict, out_dir, *, slo: Sequence[tuple[int, int]] = DEFAULT_SLO
) -> dict:
    """写 turns.jsonl / run.json / summary.md / 图;返回完整 run dict。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "turns.jsonl").open("w", encoding="utf-8") as f:
        for r in results:  # 不存 text(重,仅无损校验需要),只留指标
            f.write(json.dumps({k: v for k, v in asdict(r).items() if k != "text"}) + "\n")
    summary = summarize(
        results,
        duration_s=meta["duration_s"],
        gpu_bytes=meta["gpu"]["total_bytes"],
        num_gpus=meta["gpu"]["count"],
        slo=slo,
    )
    run = {**meta, **summary}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["plots"] = _plots(results, out)
    (out / "summary.md").write_text(_render_md(run), encoding="utf-8")
    return run


_INDEX_HEADER = (
    "# Runs\n\n"
    "| 日期 | 模型 | 配置 | 负载 | 下采样 | offered 轮/s | 完成/总 | "
    "TTFT p50 | TPOT p50 | out tok/s | good tok/s @2s,80ms | 目录 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


def _index_row(run: dict, dirname: str) -> str:
    """一行排行榜;对旧 run 缺的字段宽容(显示 '-'),容忍 schema 演进。"""
    lat, w = run.get("latency", {}), run.get("workload", {})
    ttft, tpot = lat.get("ttft_ms", {}), lat.get("tpot_ms", {})
    off = run.get("load", {}).get("offered_turn_s")
    out_tok = run.get("throughput", {}).get("output_tok_s")
    gp = run.get("goodput") or [{}]
    good = gp[0].get("good_tok_s")
    ms = lambda d, k: _fmt_ms(d[k]) if d.get(k) is not None else "-"  # noqa: E731
    num = lambda v, f="{:.0f}": f.format(v) if v is not None else "-"  # noqa: E731
    return (
        f"| {run.get('timestamp', '?')} | {run.get('model', {}).get('name', '?')} | "
        f"{run.get('tag', '?')} | {w.get('split', '')} {w.get('window', '')} | {_load_spec(w)} | "
        f"{num(off, '{:.2f}')} | {run.get('completed', '?')}/{run.get('total', '?')} | "
        f"{ms(ttft, 'p50')} | {ms(tpot, 'p50')} | {num(out_tok)} | {num(good)} | "
        f"[→]({dirname}/summary.md) |\n"
    )


def rebuild_index(runs_dir) -> None:
    """扫描 runs/*/run.json,按时间戳重建 INDEX.md(整表重写,不 append,故 schema 改了也不错位)。"""
    root = Path(runs_dir)
    runs = []
    for rj in root.glob("*/run.json"):
        try:
            runs.append((json.loads(rj.read_text(encoding="utf-8")), rj.parent.name))
        except (OSError, ValueError):
            continue
    runs.sort(key=lambda r: r[0].get("timestamp", ""))
    body = "".join(_index_row(run, name) for run, name in runs)
    (root / "INDEX.md").write_text(_INDEX_HEADER + body, encoding="utf-8")
