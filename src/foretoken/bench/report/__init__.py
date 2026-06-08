"""评测产物组装:把一次回放结果写成可复现记录 + 人读摘要。

单 run 一目录:`run.json`(配置+指标)、`turns.jsonl`(每轮原始)、`summary.md`(markdown 摘要+内嵌图)、
`en/`·`zh/`(双语图);跨 run `runs/INDEX.md` 排行榜。
拆分:`metrics.py` 纯统计、`plots.py` 出图、本模块组装。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .metrics import (
    DEFAULT_SLO,
    fmt_ms,
    goodput_ladder,
    parse_slo,
    percentiles,
    summarize,
    throughput,
)
from .plots import compare_runs, make_plots, regen_plots

__all__ = [
    "DEFAULT_SLO", "parse_slo", "summarize", "percentiles", "goodput_ladder", "throughput",
    "make_plots", "compare_runs", "regen_plots", "write_run", "rebuild_index",
]


def _load_spec(w: dict) -> str:
    """负载下采样口径(人读):n=N / sample=F / full。"""
    if w.get("n_requests"):
        return f"n={w['n_requests']}"
    if w.get("sample"):
        return f"sample={w['sample']}"
    return "full"


def _row(*cells) -> str:
    """一行 markdown 表格。"""
    return "| " + " | ".join(str(c) for c in cells) + " |"


# summary.md 内嵌图:每组按 zh 优先、否则 en
_PLOT_GROUPS = [
    ("TTFT", ["ttft_cdf", "ttft_hist"]),
    ("TPOT", ["tpot_cdf", "tpot_hist"]),
    ("输出长度", ["tokens_hist"]),
    ("时间线", ["ttft_timeline"]),
]


def _render_md(run: dict) -> str:
    """渲染 summary.md(标准 markdown:标题 + 表格 + 内嵌图)。"""
    m, gpu, w = run["model"], run["gpu"], run["workload"]
    lat, lo, tp = run["latency"], run["load"], run["throughput"]
    t, p = lat["ttft_ms"], lat["tpot_ms"]
    eng_s = ", ".join(f"{k}={v}" for k, v in m.get("engine_args", {}).items() if k != "model")
    samp_s = ", ".join(f"{k}={v}" for k, v in run["sampling"].items())
    off = lo.get("offered_turn_s")
    done, total = run["completed"], run["total"]
    pct = f"{100 * done / total:.0f}%" if total else "0%"
    dl = w.get("deadline_s")
    L = [
        f"# {m['name']} · {run['tag']} · {w['split']} {w['window']}",
        "",
        f"`{run['timestamp']}` · {run['host']} · {gpu['count']}×{gpu['name']} · "
        f"vllm {run['vllm']} · commit `{run['commit']}`",
        "",
        "## 配置",
        _row("项", "值"),
        _row("---", "---"),
        _row("engine", eng_s),
        _row("sampling", samp_s),
        _row(
            "workload",
            f"{w['split']}, window {w['window']}, {_load_spec(w)}, "
            f"sec_mult={w['sec_multiplier']}, deadline {f'{dl:.0f}s' if dl else 'off'}",
        ),
        "",
        "## 负载与完成",
        _row("项", "值"),
        _row("---", "---"),
        _row("会话 / 轮", f"{lo['sessions']} / {lo['turns']}"),
        *([_row("offered", f"{off:.2f} 轮/s")] if off else []),
        _row("完成", f"**{done}** ({pct})"),
        _row("取消", f"{run['cancelled_sessions']}(到点)"),
        _row("时长", f"{run['duration_s']:.0f}s"),
        "",
        "## 延迟",
        _row("指标", "p50", "p90", "p99"),
        _row("---", "---", "---", "---"),
        _row("TTFT", fmt_ms(t["p50"]), fmt_ms(t["p90"]), fmt_ms(t["p99"])),
        _row("TPOT", fmt_ms(p["p50"]), fmt_ms(p["p90"]), fmt_ms(p["p99"])),
        "",
        "## 吞吐",
        f"原始输出 **{tp['output_tok_s']:.0f} tok/s**({tp['output_tok_s_per_gpu']:.0f}/GPU)"
        f" · 完成 **{tp['request_s']:.2f} req/s**",
        "",
        "## goodput(SLO 达成阶梯)",
        _row("SLO (TTFT, TPOT)", "达成%", "good tok/s", "/GPU", "tok/(s·GPU字节)"),
        _row("---", "---", "---", "---", "---"),
    ]
    for g in run["goodput"]:
        norm = "-" if g["tok_per_s_gpubyte"] is None else f"{g['tok_per_s_gpubyte']:.2e}"
        L.append(
            _row(
                f"{fmt_ms(g['ttft_ms'])}, {g['tpot_ms']}ms",
                f"{100 * g['attain']:.0f}%",
                f"{g['good_tok_s']:.0f}",
                f"{g['good_tok_s_per_gpu']:.0f}",
                norm,
            )
        )
    plots = run.get("plots", [])
    if plots:

        def pick(b):  # 该图优先 zh、否则 en(图在 <run>/<lang>/ 子目录)
            return next((f"{lg}/{b}.png" for lg in ("zh", "en") if f"{lg}/{b}.png" in plots), None)

        L += ["", "## 图"]
        for title, bases in _PLOT_GROUPS:
            imgs = [i for i in (pick(b) for b in bases) if i]
            if imgs:
                L.append(f"### {title}")
                L += [f"![{i}]({i})" for i in imgs]
    return "\n".join(L) + "\n"


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
    run["plots"] = make_plots(results, out, slo=slo)
    (out / "summary.md").write_text(_render_md(run), encoding="utf-8")
    return run


_INDEX_HEADER = (
    "# Runs\n\n"
    "| 日期 | 模型 | 配置 | 负载 | 下采样 | offered 轮/s | 完成/总 | "
    "TTFT p50 | TPOT p50 | out tok/s | good tok/s @strict SLO | 目录 |\n"
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
    ms = lambda d, k: fmt_ms(d[k]) if d.get(k) is not None else "-"  # noqa: E731
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
