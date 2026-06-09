# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""多组实验对比:全指标表 + 对比图(CDF 叠加 / 分位柱 / goodput / 吞吐),写 compare/summary.md。

入口 `compare(run_dirs)`;读各 run 的 run.json(聚合指标)+ turns.jsonl(分布),双语出图。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from .metrics import fmt_ms
from .plots import _PALETTE, _apply_fmt, _fig_ctx, _fmt_k, _langs, _resolve_cjk_font

_T = {  # 对比图双语词表
    "cdf_y": {"en": "CDF", "zh": "累积占比"},
    "cmp": {"en": "comparison", "zh": "对照"},
    "lat_y": {"en": "latency", "zh": "延迟"},
    "good_y": {"en": "goodput (tok/s)", "zh": "goodput (tok/s)"},
    "tps_y": {"en": "throughput (tok/s)", "zh": "吞吐 (tok/s)"},
    "ttft": {"en": "TTFT", "zh": "TTFT"},
    "tpot": {"en": "TPOT", "zh": "TPOT"},
    "e2e": {"en": "E2E", "zh": "端到端"},
    "pctl": {"en": "percentile", "zh": "分位"},
    "good": {"en": "goodput by SLO tier", "zh": "goodput(SLO 阶梯)"},
    "strict": {"en": "strict", "zh": "严"},
    "medium": {"en": "medium", "zh": "中"},
    "loose": {"en": "loose", "zh": "松"},
    "raw_vs_good": {"en": "raw output vs goodput (strict)", "zh": "原始输出 vs goodput(严)"},
    "raw": {"en": "raw output", "zh": "原始输出"},
    "good_strict": {"en": "goodput (strict)", "zh": "goodput(严)"},
}
_LAT = [("ttft_ms", "ttft"), ("tpot_ms", "tpot"), ("e2e_ms", "e2e")]


def _run_label(run: dict) -> str:
    """对比里区分各 run 的短标签:tag·window·(r<rate>|t<total>)。"""
    w = run.get("workload", {})
    parts = [str(run.get("tag", "?"))]
    if w.get("window"):
        parts.append(str(w["window"]))
    if w.get("rate_per_min"):
        parts.append(f"r{w['rate_per_min']:g}")
    elif w.get("total_requests"):
        parts.append(f"t{w['total_requests']}")
    return "·".join(parts)


def _load(run_dirs) -> list[tuple[str, dict, list[dict]]]:
    """读 (label, run.json, ok 轮原始);标签去重(撞了加 #n)。"""
    loaded: list[list] = []
    for d in run_dirs:
        rj = Path(d) / "run.json"
        if not rj.exists():
            continue
        run = json.loads(rj.read_text(encoding="utf-8"))
        tj = Path(d) / "turns.jsonl"
        rows = (
            [json.loads(s) for s in tj.read_text(encoding="utf-8").splitlines() if s.strip()]
            if tj.exists()
            else []
        )
        loaded.append([_run_label(run), run, [r for r in rows if r.get("ok")]])
    seen: dict[str, int] = {}
    for item in loaded:
        k = item[0]
        seen[k] = n = seen.get(k, 0) + 1
        if n > 1:
            item[0] = f"{item[0]}#{n}"
    return [(lbl, run, ok) for lbl, run, ok in loaded]


def compare_table(loaded) -> str:
    """全指标对比表(markdown):延迟分位 / 尾部 / 吞吐 / goodput 三档+达成 / KV / 并发。"""
    def num(v, f="{:.0f}"):
        return f.format(v) if isinstance(v, int | float) else "-"

    def lat3(d):
        g = lambda k: fmt_ms(d[k]) if d and d.get(k) is not None else "-"  # noqa: E731
        return f"{g('p50')} / {g('p90')} / {g('p99')}"

    def gpc(g):
        return f"{num(g.get('good_tok_s'))} ({num(g.get('attain', 0) * 100)}%)" if g else "-"

    head = [
        "run", "完成", "TTFT p50/p90/p99", "TPOT p50/p90/p99", "E2E p50/p90/p99", "尾部",
        "out tok/s", "req/s", "good@严(达成)", "good@中", "good@松", "峰值KV", "运行中", "排队",
    ]
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for lbl, run, _ in loaded:
        lat = run.get("latency", {})
        tp = run.get("throughput", {})
        gp = _gp(run)
        eng = run.get("engine") or {}
        kv = f"{eng['peak_kv'] * 100:.0f}%" if eng.get("peak_kv") is not None else "-"
        cells = [
            lbl, f"{run.get('completed', '?')}/{run.get('total', '?')}",
            lat3(lat.get("ttft_ms", {})), lat3(lat.get("tpot_ms", {})), lat3(lat.get("e2e_ms", {})),
            f"{run.get('tail_ratio_ttft', 0):.1f}×",
            num(tp.get("output_tok_s")), num(tp.get("request_s"), "{:.2f}"),
            gpc(gp[0]), gpc(gp[1]), gpc(gp[2]), kv,
            num(eng.get("max_running")), num(eng.get("max_waiting")),
        ]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def _gp(run: dict) -> list[dict]:
    """goodput 三档(严/中/松),不足补空。"""
    return ((run.get("goodput") or []) + [{}, {}, {}])[:3]


def _bars(ax, groups, series):
    """分组柱:groups=x 标签,series=[(label, [每组值])]。"""
    x = np.arange(len(groups))
    w = 0.8 / max(len(series), 1)
    for i, (label, vals) in enumerate(series):
        off = (i - (len(series) - 1) / 2) * w
        ax.bar(x + off, vals, w, label=label, color=_PALETTE[i % len(_PALETTE)])
    ax.set_xticks(x)
    ax.set_xticklabels(groups)


def _plots(loaded, out: Path) -> list[str]:
    """出对比图(双语):3 延迟 CDF 叠加 + 3 延迟分位柱 + goodput 柱 + 原始-vs-good 吞吐柱。"""
    cjk = _resolve_cjk_font()
    made: list[str] = []

    def emit(fig, base, lang):
        fig.tight_layout()
        sub = out / lang
        sub.mkdir(parents=True, exist_ok=True)
        fig.savefig(sub / f"{base}.png")
        plt.close(fig)
        made.append(f"{lang}/{base}.png")

    for lang, font in _langs(cjk):
        for attr, key in _LAT:  # CDF 叠加
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(6.4, 4))
                for lbl, _run, ok in loaded:
                    xs = sorted(r[attr] for r in ok if r.get(attr, 0) > 0)
                    if xs:
                        ys = [(i + 1) / len(xs) for i in range(len(xs))]
                        ax.plot(xs, ys, label=f"{lbl} (p50={fmt_ms(xs[len(xs) // 2])})")
                ax.set_xscale("log")
                _apply_fmt(ax, xfn=fmt_ms)
                ax.set_xlabel(_T[key][lang])
                ax.set_ylabel(_T["cdf_y"][lang])
                ax.set_title(f"{_T[key][lang]} {_T['cmp'][lang]}")
                ax.legend(loc="lower right")
                emit(fig, f"cmp_{key}_cdf", lang)

        for attr, key in _LAT:  # 分位柱(p50/p90/p99)
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(6.4, 4))
                pcts = ["p50", "p90", "p99"]
                series = [
                    (lbl, [run.get("latency", {}).get(attr, {}).get(p, 0) for p in pcts])
                    for lbl, run, _ in loaded
                ]
                _bars(ax, pcts, series)
                _apply_fmt(ax, yfn=fmt_ms)
                ax.set_xlabel(_T["pctl"][lang])
                ax.set_ylabel(f"{_T[key][lang]} {_T['lat_y'][lang]}")
                ax.set_title(f"{_T[key][lang]} {_T['pctl'][lang]} {_T['cmp'][lang]}")
                ax.legend(loc="upper left")
                emit(fig, f"cmp_{key}_pctl", lang)

        with _fig_ctx(plt, font):  # goodput 三档柱
            fig, ax = plt.subplots(figsize=(6.4, 4))
            tiers = [_T["strict"][lang], _T["medium"][lang], _T["loose"][lang]]
            series = [
                (lbl, [g.get("good_tok_s", 0) for g in _gp(run)]) for lbl, run, _ in loaded
            ]
            _bars(ax, tiers, series)
            _apply_fmt(ax, yfn=_fmt_k)
            ax.set_ylabel(_T["good_y"][lang])
            ax.set_title(f"{_T['good'][lang]} {_T['cmp'][lang]}")
            ax.legend(loc="upper right")
            emit(fig, "cmp_goodput", lang)

        with _fig_ctx(plt, font):  # 原始输出 vs goodput(严)
            fig, ax = plt.subplots(figsize=(6.4, 4))
            labels = [lbl for lbl, _, _ in loaded]
            raw = [run.get("throughput", {}).get("output_tok_s", 0) for _, run, _ in loaded]
            good = [((run.get("goodput") or [{}])[0]).get("good_tok_s", 0) for _, run, _ in loaded]
            _bars(ax, labels, [(_T["raw"][lang], raw), (_T["good_strict"][lang], good)])
            _apply_fmt(ax, yfn=_fmt_k)
            ax.set_ylabel(_T["tps_y"][lang])
            ax.set_title(f"{_T['raw_vs_good'][lang]} {_T['cmp'][lang]}")
            ax.legend(loc="upper right")
            ax.tick_params(axis="x", labelrotation=20)
            emit(fig, "cmp_raw_vs_good", lang)
    return made


_GROUPS = [
    ("延迟 CDF 叠加", ["cmp_ttft_cdf", "cmp_tpot_cdf", "cmp_e2e_cdf"]),
    ("延迟分位柱(p50/p90/p99)", ["cmp_ttft_pctl", "cmp_tpot_pctl", "cmp_e2e_pctl"]),
    ("goodput / 吞吐", ["cmp_goodput", "cmp_raw_vs_good"]),
]


def _cost_section(loaded, out: Path) -> list[str]:
    """把计价成本(表 + 双语柱图)一并并入对比报告;时价用 cost 模块默认(单独定制走 --cost)。"""
    from .cost import DEFAULT_RATES, _cost_plot, cost_rows, cost_table  # 局部导入:cost 反向依赖本模块

    rows = cost_rows(loaded, rates=DEFAULT_RATES, slo_tier=0)
    slo = None
    for _, run, _ in loaded:  # 严档 SLO 阈值用于表头
        t = _gp(run)[0]
        if t:
            slo = (t.get("ttft_ms"), t.get("tpot_ms"))
            break
    plots = _cost_plot(rows, out)
    img = next((p for p in plots if p.startswith("zh")), plots[0] if plots else None)
    md = ["## 计价成本(¥/Mtok)", cost_table(rows, slo)]
    if img:
        md += [f"![{img}]({img})", ""]
    return md


def compare(run_dirs, out_dir=None) -> Path | None:
    """对比多组实验 → 写 <out>/summary.md(全指标表 + 内嵌图 + 计价成本)+ 双语图;返回 out 目录。

    out_dir 缺省 `results/compare/<时间戳>`(每次独立留存、不覆盖);sh 传 out_dir 可指定。
    """
    dirs = list(run_dirs)
    if len(dirs) == 1 and not (Path(dirs[0]) / "run.json").exists():  # 单个 runs 根 → 展开子 run
        dirs = sorted(str(p.parent) for p in Path(dirs[0]).glob("*/run.json"))
    loaded = _load(dirs)
    if len(loaded) < 2:
        return None
    out = Path(out_dir) if out_dir else Path("results/compare") / datetime.now().strftime(
        "%Y-%m-%d_%H%M%S"
    )
    out.mkdir(parents=True, exist_ok=True)
    plots = _plots(loaded, out)

    def pick(b):  # 内嵌图优先 zh
        return next((f"{lg}/{b}.png" for lg in ("zh", "en") if f"{lg}/{b}.png" in plots), None)

    md = [f"# 实验对比({len(loaded)} 组)", "", compare_table(loaded)]
    for title, bases in _GROUPS:
        imgs = [i for i in (pick(b) for b in bases) if i]
        if imgs:
            md += [f"## {title}", *[f"![{i}]({i})" for i in imgs], ""]
    md += _cost_section(loaded, out)  # 成本表 + 成本图一并并入对比报告
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out
