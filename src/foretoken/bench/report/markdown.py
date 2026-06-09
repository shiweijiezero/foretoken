# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""人读文本渲染:单 run 的 summary.md、跨 run 的 INDEX.md 排行榜。"""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import fmt_ms


def _load_spec(w: dict) -> str:
    """负载下采样口径(人读):total_requests=N(@rate/min)/ sample=F / full。"""
    total = w.get("total_requests") or w.get("n_requests")  # 兼容旧 run
    if total:
        r = w.get("rate_per_min")
        return f"total_requests={total}" + (f"@{r:g}/min" if r else "")
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


def render_summary(run: dict) -> str:
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
        f" · 输出长度均值 {run.get('output_len', {}).get('mean', 0):.0f} tok"
        + (f" · 生成速度(1000/TPOT)p50 **{g['p50']:.0f}** tok/s(慢尾 p10 {g['p10']:.0f})"
           if (g := lat.get("gen_tok_s")) else ""),
        "",
        "## 吞吐",
        f"输出 **{tp['output_tok_s']:.0f} tok/s**({tp['output_tok_s_per_gpu']:.0f}/GPU)"
        f" · 总(含输入) **{tp.get('total_tok_s', 0):.0f} tok/s**"
        f" · 完成 **{tp['request_s']:.2f} req/s**",
        "",
        "## goodput(SLO 达成阶梯)",
        _row("SLO (TTFT, TPOT)", "达成%", "good tok/s", "/GPU", "¥/Mtok", "tok/(s·GPU字节)"),
        _row("---", "---", "---", "---", "---", "---"),
    ]
    cpm = (run.get("cost") or {}).get("cny_per_mtok") or []
    for i, g in enumerate(run["goodput"]):
        norm = "-" if g["tok_per_s_gpubyte"] is None else f"{g['tok_per_s_gpubyte']:.2e}"
        cny = f"¥{cpm[i]:.2f}" if i < len(cpm) and cpm[i] is not None else "-"
        L.append(
            _row(
                f"{fmt_ms(g['ttft_ms'])}, {g['tpot_ms']}ms",
                f"{100 * g['attain']:.0f}%",
                f"{g['good_tok_s']:.0f}",
                f"{g['good_tok_s_per_gpu']:.0f}",
                cny,
                norm,
            )
        )
    c = run.get("cost") or {}
    if c.get("run_cost_cny") is not None:
        raw = c.get("cny_per_mtok_raw")
        L += [
            "",
            f"成本(¥{c['gpu_rate_cny_h']:g}/GPU·h):运行 **¥{c['run_cost_cny']:.2f}**"
            f" · 原始 **{f'¥{raw:.2f}' if raw else '-'}/Mtok**(达成口径见上表)",
        ]
    eng = run.get("engine")
    if eng:
        extra = []
        if eng.get("prefix_hit_rate") is not None:
            extra.append(f"prefix 命中率 **{100 * eng['prefix_hit_rate']:.0f}%**")
        if eng.get("decode_tok_s") is not None:
            extra.append(f"引擎 decode **{eng['decode_tok_s']:.0f} tok/s**")
        extra.append(f"抢占 **{eng.get('total_preempted', 0)}**")
        L += [
            "",
            "## 引擎(逐 iteration)",
            f"峰值 KV 利用率 **{100 * eng['peak_kv']:.0f}%**(均值 {100 * eng['mean_kv']:.0f}%)"
            f" · 最大运行中 **{eng['max_running']}** · 最大排队 **{eng['max_waiting']}**"
            + (" · " + " · ".join(extra) if extra else ""),
        ]
    er = run.get("engine_requests")
    if er:
        rs = " · ".join(f"{k or '?'}={v}" for k, v in er["finish_reason"].items())
        frac = er.get("cached_token_frac")
        L += [
            "",
            "## 引擎侧请求分解(完成请求,p50 / p99)",
            f"排队 **{fmt_ms(er['queued_ms']['p50'])} / {fmt_ms(er['queued_ms']['p99'])}**"
            f" · prefill {fmt_ms(er['prefill_ms']['p50'])} / {fmt_ms(er['prefill_ms']['p99'])}"
            f" · decode {fmt_ms(er['decode_ms']['p50'])} / {fmt_ms(er['decode_ms']['p99'])}"
            f" · 引擎 TPOT {fmt_ms(er['tpot_ms']['p50'])} / {fmt_ms(er['tpot_ms']['p99'])}",
            f"结束原因:{rs}"
            + (f" · 缓存命中 token 占比 {100 * frac:.0f}%" if frac is not None else ""),
        ]
    ch = run.get("client_health") or {}
    if ch.get("samples"):
        verdict = "可能是瓶颈(发送侧饱和)" if ch["loop_lag_max_ms"] > 100 else "客户端非瓶颈"
        L += [
            "",
            "## 客户端健康(事件循环延迟,漂移大 = 单事件循环饱和、瓶颈在发送侧而非引擎)",
            f"均值 **{ch['loop_lag_mean_ms']:.0f}ms** · p99 **{ch['loop_lag_p99_ms']:.0f}ms**"
            f" · 峰值 **{ch['loop_lag_max_ms']:.0f}ms** —— {verdict}",
        ]
    cases = run.get("cases", "full")
    if cases != "off":
        line = "样例输入输出:`cases.md`(可读样例)"
        line += " + `cases.jsonl`(全量)。" if cases == "full" else "(`--cases full` 另出全量)。"
        L += ["", line]
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


_INDEX_HEADER = (
    "# Runs\n\n"
    "| 日期 | 模型 | 配置 | 负载 | 下采样 | offered 轮/s | 完成/总 | "
    "TTFT p50 | TPOT p50 | out tok/s | good tok/s @strict SLO | ¥/Mtok @strict | 目录 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


def _index_row(run: dict, dirname: str) -> str:
    """一行排行榜;对旧 run 缺的字段宽容(显示 '-'),容忍 schema 演进。"""
    lat, w = run.get("latency", {}), run.get("workload", {})
    ttft, tpot = lat.get("ttft_ms", {}), lat.get("tpot_ms", {})
    off = run.get("load", {}).get("offered_turn_s")
    out_tok = run.get("throughput", {}).get("output_tok_s")
    gp = run.get("goodput") or [{}]
    good = gp[0].get("good_tok_s")
    cpm = (run.get("cost") or {}).get("cny_per_mtok") or [None]
    ms = lambda d, k: fmt_ms(d[k]) if d.get(k) is not None else "-"  # noqa: E731
    num = lambda v, f="{:.0f}": f.format(v) if v is not None else "-"  # noqa: E731
    cny = f"¥{cpm[0]:.2f}" if cpm[0] is not None else "-"
    return (
        f"| {run.get('timestamp', '?')} | {run.get('model', {}).get('name', '?')} | "
        f"{run.get('tag', '?')} | {w.get('split', '')} {w.get('window', '')} | {_load_spec(w)} | "
        f"{num(off, '{:.2f}')} | {run.get('completed', '?')}/{run.get('total', '?')} | "
        f"{ms(ttft, 'p50')} | {ms(tpot, 'p50')} | {num(out_tok)} | {num(good)} | {cny} | "
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
