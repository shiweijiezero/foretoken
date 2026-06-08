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
    ("E2E", ["e2e_cdf", "e2e_hist"]),
    ("输出 / 输入长度", ["tokens_hist", "prompt_hist"]),
    ("时间线 / 吞吐", ["ttft_timeline", "throughput_timeline"]),
    ("KV 利用率 / 并发", ["kv_timeline", "concurrency_timeline"]),
]


def _render_md(run: dict) -> str:
    """渲染 summary.md(标准 markdown:标题 + 表格 + 内嵌图)。"""
    m, gpu, w = run["model"], run["gpu"], run["workload"]
    lat, lo, tp = run["latency"], run["load"], run["throughput"]
    t, p, e = lat["ttft_ms"], lat["tpot_ms"], lat.get("e2e_ms", {})
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
        *([_row("E2E", fmt_ms(e["p50"]), fmt_ms(e["p90"]), fmt_ms(e["p99"]))] if e else []),
        "",
        f"尾部比 TTFT p99/p50 = **{run.get('tail_ratio_ttft', 0):.1f}×**"
        f" · 输出长度均值 {run.get('output_len', {}).get('mean', 0):.0f} tok",
        "",
        "## 吞吐",
        f"输出 **{tp['output_tok_s']:.0f} tok/s**({tp['output_tok_s_per_gpu']:.0f}/GPU)"
        f" · 总(含输入) **{tp.get('total_tok_s', 0):.0f} tok/s**"
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
    eng = run.get("engine")
    if eng:
        L += [
            "",
            "## 引擎(逐 iteration)",
            f"峰值 KV 利用率 **{100 * eng['peak_kv']:.0f}%**(均值 {100 * eng['mean_kv']:.0f}%)"
            f" · 最大在飞 **{eng['max_running']}** · 最大排队 **{eng['max_waiting']}**",
        ]
    L += ["", "样例输入输出:`cases.jsonl`(全量)/ `cases.md`(可读样例)。"]
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


_HEAVY = {"text", "user", "system"}  # 不入 turns.jsonl(指标表)的重字段;另存 cases


def _write_cases(results, out: Path, max_md_sessions: int = 5) -> None:
    """写每轮输入输出:cases.jsonl(全量,机器/grep)+ cases.md(前若干会话,可读)。"""
    with (out / "cases.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            d = asdict(r)
            f.write(
                json.dumps(
                    {k: d[k] for k in ("session_id", "turn", "system", "user", "ok")}
                    | {"assistant": d["text"]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    by_sid: dict = {}
    for r in results:
        by_sid.setdefault(r.session_id, []).append(r)
    md = ["# 样例输入输出(前若干会话;完整见 cases.jsonl)"]
    for sid in list(by_sid)[:max_md_sessions]:
        md.append(f"\n## session {sid}")
        for r in sorted(by_sid[sid], key=lambda x: x.turn):
            if r.turn == 0 and r.system:
                md.append(f"\n**system**\n\n{r.system}")
            md.append(f"\n**user**\n\n{r.user}")
            md.append(f"\n**assistant**\n\n{r.text}")
    (out / "cases.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def write_run(
    results,
    meta: dict,
    out_dir,
    *,
    slo: Sequence[tuple[int, int]] = DEFAULT_SLO,
    engine_stats: list[dict] | None = None,
) -> dict:
    """写 turns.jsonl / cases / engine_stats / run.json / summary.md / 图;返回完整 run dict。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "turns.jsonl").open("w", encoding="utf-8") as f:
        for r in results:  # 指标表:剔除重字段(输入输出另存 cases)
            f.write(json.dumps({k: v for k, v in asdict(r).items() if k not in _HEAVY}) + "\n")
    _write_cases(results, out)
    if engine_stats:
        with (out / "engine_stats.jsonl").open("w", encoding="utf-8") as f:
            for s in engine_stats:
                f.write(json.dumps(s) + "\n")
    summary = summarize(
        results,
        duration_s=meta["duration_s"],
        gpu_bytes=meta["gpu"]["total_bytes"],
        num_gpus=meta["gpu"]["count"],
        slo=slo,
        engine_stats=engine_stats,
    )
    run = {**meta, **summary}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["plots"] = make_plots(results, out, slo=slo, engine_stats=engine_stats)
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
