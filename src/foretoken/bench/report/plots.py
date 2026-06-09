# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""出图:每指标 CDF + 直方图、输出长度分布、TTFT 随到达时刻散点,跨 run 对照。

全部从 saved per-turn 数据(turns.jsonl)生成,en + zh 双份(`<run>/en/`、`<run>/zh/`);
无 CJK 字体只出 en。SLO 参考线取 `slo` 阶梯最严档。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from .metrics import fmt_ms

matplotlib.use("Agg")  # 固定 Agg 后端(无显示环境出图);须在创建任何 figure 前

# 已注册字体里按序挑中文字体(覆盖各平台常见 CJK 字体名;无则只出 en)
_CJK_FONTS = [
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei", "PingFang SC", "SimHei",
]
_T = {  # 双语词表
    "cdf": {"en": "CDF", "zh": "累积分布"},
    "hist": {"en": "distribution", "zh": "分布"},
    "cdf_y": {"en": "CDF", "zh": "累积占比"},
    "hist_y": {"en": "count", "zh": "频数"},
    "slo": {"en": "SLO", "zh": "SLO"},
    "time_x": {"en": "arrival time (s)", "zh": "到达时刻 (s)"},
    "timeline": {"en": "TTFT over arrival time", "zh": "TTFT 随到达时刻"},
    "tput_x": {"en": "time (s)", "zh": "时刻 (s)"},
    "tput_y": {"en": "output tokens/s", "zh": "输出 tok/s"},
    "tput": {"en": "output throughput over time", "zh": "输出吞吐随时间"},
    "tput_good": {"en": "goodput (SLO-met)", "zh": "goodput(达标)"},
    "cmp": {"en": "comparison", "zh": "对照"},
    "time_s": {"en": "time (s)", "zh": "时刻 (s)"},
    "kv_y": {"en": "KV cache usage (%)", "zh": "KV 利用率 (%)"},
    "kv_title": {"en": "KV cache usage over time", "zh": "KV 利用率随时间"},
    "conc_y": {"en": "requests", "zh": "请求数"},
    "conc_title": {"en": "concurrency over time", "zh": "并发随时间"},
    "running": {"en": "running", "zh": "运行中"},
    "waiting": {"en": "waiting", "zh": "排队"},
    "rate_x": {"en": "arrival rate (req/min)", "zh": "到达率 (req/min)"},
    "total_x": {"en": "requests", "zh": "请求总数"},
    "win_x": {"en": "window (min)", "zh": "窗口 (min)"},
    "tps_y": {"en": "throughput (tokens/s)", "zh": "吞吐 (tok/s)"},
    "attain_y": {"en": "SLO attainment (%)", "zh": "SLO 达成率 (%)"},
    "raw_tput": {"en": "raw output", "zh": "原始输出"},
    "gp_strict": {"en": "goodput (strict)", "zh": "goodput(严)"},
    "gp_medium": {"en": "goodput (medium)", "zh": "goodput(中)"},
    "gp_loose": {"en": "goodput (loose)", "zh": "goodput(松)"},
    "sweep_title": {"en": "goodput vs load", "zh": "goodput 随负载"},
    "knee": {"en": "goodput peak", "zh": "goodput 峰值"},
}


def _decode_intervals(turns):
    """每轮 decode 区间 [首 token 时刻, 完成时刻](秒)+ tokens;需 send_ms(旧 run 无则跳过)。"""
    iv = []
    for r in turns:
        s0 = getattr(r, "send_ms", 0)
        if not s0:
            continue
        s = (s0 + r.ttft_ms) / 1000.0
        e = getattr(r, "complete_ms", 0) / 1000.0
        if e > s and r.output_tokens > 0:
            iv.append((s, e, r.output_tokens))
    return iv


def _bin_rate(iv, t0: float, t1: float, n_bins: int):
    """各区间 tokens 按重叠均摊到 [t0,t1] 的 n_bins 桶 → (桶中心时刻 s, 每桶 tok/s)。

    raw 与 goodput 传同一 (t0,t1,n_bins) 即落在同一时间网格、可叠同图。空区间返回全 0。
    """
    width = (t1 - t0) / n_bins
    rate = [0.0] * n_bins
    for s, e, tok in iv:
        per_s = tok / (e - s)  # 该轮平均产出速率
        b0 = max(0, int((s - t0) / width))
        b1 = min(n_bins - 1, int((e - t0) / width))
        for b in range(b0, b1 + 1):
            lo = t0 + b * width
            ov = min(e, lo + width) - max(s, lo)  # 与该桶的重叠秒数
            if ov > 0:
                rate[b] += per_s * ov / width
    return [t0 + (b + 0.5) * width for b in range(n_bins)], rate
# 指标:attr, 文件前缀, 名{en,zh}, x 轴{en,zh}, log_x, 出 CDF?(SLO 参考线由 _slo_ref 从 slo 取)
_METRICS = [
    ("ttft_ms", "ttft", {"en": "TTFT", "zh": "TTFT"},
     {"en": "TTFT (ms)", "zh": "首 token 延迟 TTFT (ms)"}, True, True),
    ("tpot_ms", "tpot", {"en": "TPOT", "zh": "TPOT"},
     {"en": "TPOT (ms)", "zh": "每 token 延迟 TPOT (ms)"}, True, True),
    ("e2e_ms", "e2e", {"en": "E2E", "zh": "端到端"},
     {"en": "E2E latency (ms)", "zh": "端到端延迟 (ms)"}, True, True),
    ("output_tokens", "tokens", {"en": "output length", "zh": "输出长度"},
     {"en": "output tokens", "zh": "输出 token 数"}, True, False),
    ("prompt_tokens", "prompt", {"en": "prompt length", "zh": "输入长度"},
     {"en": "prompt tokens", "zh": "输入 token 数"}, True, False),
]


def _slo_ref(attr: str, slo: Sequence[tuple[int, int]]) -> int | None:
    """该指标的 SLO 参考线 ms(取阶梯最严档:TTFT→slo[0][0]、TPOT→slo[0][1]);无则 None。"""
    if not slo:
        return None
    return {"ttft_ms": slo[0][0], "tpot_ms": slo[0][1]}.get(attr)


def _resolve_cjk_font() -> str | None:
    """可用中文字体名:env FORETOKEN_CJK_FONT(名或 ttf 路径)优先,否则在已注册字体里挑;无 → None。"""
    import os

    from matplotlib import font_manager as fm

    env = os.environ.get("FORETOKEN_CJK_FONT")
    if env:
        if Path(env).exists():
            fm.fontManager.addfont(env)
            return fm.FontProperties(fname=env).get_name()
        return env
    avail = {f.name for f in fm.fontManager.ttflist}
    return next((c for c in _CJK_FONTS if c in avail), None)


def _langs(cjk: str | None) -> list[tuple[str, str | None]]:
    return [("en", None), *([("zh", cjk)] if cjk else [])]


# 专业配色 + 论文级样式(去顶/右边框、柔和网格、克制配色、150dpi、tight 裁剪)
_PALETTE = ["#2c6fbb", "#d1495b", "#2a9d8f", "#e9a23b", "#6a4c93", "#8c8c8c"]
_HIST = "#5b8fc9"
_STYLE = {
    "figure.dpi": 150, "savefig.dpi": 150, "savefig.bbox": "tight", "savefig.pad_inches": 0.06,
    "figure.facecolor": "white", "font.size": 11, "axes.titlesize": 11.5, "axes.labelsize": 10.5,
    "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 8.5,
    "axes.linewidth": 0.8, "axes.edgecolor": "#444444", "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": "#cccccc", "grid.linewidth": 0.6, "grid.alpha": 0.5,
    "lines.linewidth": 1.9, "lines.solid_capstyle": "round", "legend.frameon": False,
    "xtick.direction": "out", "ytick.direction": "out", "axes.formatter.useoffset": False,
}


def _fig_ctx(plt, font):
    from cycler import cycler

    rc = dict(_STYLE, **{"axes.prop_cycle": cycler(color=_PALETTE), "axes.unicode_minus": False})
    if font:
        rc["font.sans-serif"] = [font]
    return plt.rc_context(rc)


def _fmt_k(v) -> str:
    """大数缩写:1000→1k、1.5e6→1.5M;小数普通(避免 100000 这类长数字)。"""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:g}M"
    if a >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def _apply_fmt(ax, xfn=None, yfn=None):
    """给指定轴设领域格式器(log 轴尤其需要:否则 decade 标成长数字);minor 不标。"""
    from matplotlib.ticker import FuncFormatter, NullFormatter

    for axis, fn in ((ax.xaxis, xfn), (ax.yaxis, yfn)):
        if fn is None:
            continue
        axis.set_major_formatter(FuncFormatter(lambda v, _, f=fn: f(v)))
        axis.set_minor_formatter(NullFormatter())


# 各指标 x 轴的领域格式器:延迟→ms 人读(1000→1s)、token 数→k 缩写
_XFMT = {
    "ttft_ms": fmt_ms, "tpot_ms": fmt_ms, "e2e_ms": fmt_ms,
    "output_tokens": _fmt_k, "prompt_tokens": _fmt_k,
}


def _vline(ax, x, lang):
    """竖直 SLO 参考线 + 标注(y 用 axes 比例,CDF / 直方图通用)。"""
    ax.axvline(x, ls="--", color="gray", lw=1)
    ax.text(
        x, 0.96, f" {fmt_ms(x)} {_T['slo'][lang]}", color="gray", fontsize=8,
        transform=ax.get_xaxis_transform(), va="top",
    )


def make_plots(
    results, out: Path, *, slo: Sequence[tuple[int, int]], engine_stats=None
) -> list[str]:
    """每指标 CDF + 直方图、E2E、输入/输出长度、时间线、吞吐、引擎 KV%/并发随时间;en + zh。"""
    cjk = _resolve_cjk_font()
    ok = [r for r in results if r.ok]
    made: list[str] = []
    for p in out.glob("*.png"):  # 清旧的扁平布局图(现按 en/ zh/ 分目录)
        p.unlink()

    def emit(fig, base, lang):  # 按语言分目录:<run>/<lang>/<base>.png
        fig.tight_layout()
        (out / lang).mkdir(exist_ok=True)
        fig.savefig(out / lang / f"{base}.png")
        plt.close(fig)
        made.append(f"{lang}/{base}.png")

    for attr, base, name, xlab, log_x, do_cdf in _METRICS:
        xs = sorted(v for v in (getattr(r, attr, 0) for r in ok) if v and v > 0)
        if not xs:
            continue
        slo_ms, xfn = _slo_ref(attr, slo), _XFMT.get(attr)
        ys = [(i + 1) / len(xs) for i in range(len(xs))]
        bins = np.logspace(np.log10(xs[0]), np.log10(xs[-1]), 40) if xs[-1] > xs[0] else "auto"
        for lang, font in _langs(cjk):
            if do_cdf:
                with _fig_ctx(plt, font):
                    fig, ax = plt.subplots(figsize=(5, 3.2))
                    ax.plot(xs, ys)
                    if slo_ms:
                        _vline(ax, slo_ms, lang)
                    ax.set_xscale("log")
                    _apply_fmt(ax, xfn=xfn)
                    ax.set_xlabel(xlab[lang])
                    ax.set_ylabel(_T["cdf_y"][lang])
                    ax.set_title(f"{name[lang]} {_T['cdf'][lang]} (n={len(xs)})")
                    emit(fig, f"{base}_cdf", lang)
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.hist(xs, bins=bins, color=_HIST, edgecolor="white", linewidth=0.3)
                if slo_ms:
                    _vline(ax, slo_ms, lang)
                if log_x:
                    ax.set_xscale("log")
                    _apply_fmt(ax, xfn=xfn)
                ax.set_xlabel(xlab[lang])
                ax.set_ylabel(_T["hist_y"][lang])
                ax.set_title(f"{name[lang]} {_T['hist'][lang]} (n={len(xs)})")
                emit(fig, f"{base}_hist", lang)

    ref = _slo_ref("ttft_ms", slo)
    pts = [(r.send_ms / 1000.0, r.ttft_ms) for r in ok if getattr(r, "send_ms", 0)]
    if pts:
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=10, alpha=0.55)
                if ref:
                    ax.axhline(ref, ls="--", color="#888888", lw=1)
                ax.set_yscale("log")
                _apply_fmt(ax, yfn=fmt_ms)
                ax.set_xlabel(_T["time_x"][lang])
                ax.set_ylabel("TTFT")
                ax.set_title(f"{_T['timeline'][lang]} (n={len(pts)})")
                emit(fig, "ttft_timeline", lang)

    iv_all = _decode_intervals(ok)  # 吞吐随时间:原始输出 vs goodput(只计达标轮)叠同图
    if iv_all:
        t0, t1, n_bins = min(s for s, _, _ in iv_all), max(e for _, e, _ in iv_all), 80
        if t1 > t0:
            xs_t, raw = _bin_rate(iv_all, t0, t1, n_bins)
            strict = slo[0] if slo else None  # 最严档 (TTFT_ms, TPOT_ms)
            good_turns = [
                r for r in ok if strict and r.ttft_ms <= strict[0] and r.tpot_ms <= strict[1]
            ]
            _, good = _bin_rate(_decode_intervals(good_turns), t0, t1, n_bins)
            for lang, font in _langs(cjk):
                with _fig_ctx(plt, font):
                    fig, ax = plt.subplots(figsize=(5, 3.2))
                    ax.plot(xs_t, raw, color="#8c8c8c", label=_T["raw_tput"][lang])
                    ax.fill_between(xs_t, raw, alpha=0.10, color="#8c8c8c")
                    ax.plot(xs_t, good, color=_PALETTE[0], label=_T["tput_good"][lang])
                    ax.fill_between(xs_t, good, alpha=0.22, color=_PALETTE[0])
                    _apply_fmt(ax, yfn=_fmt_k)
                    ax.set_xlabel(_T["tput_x"][lang])
                    ax.set_ylabel(_T["tput_y"][lang])
                    ax.set_title(f"{_T['tput'][lang]} (n={len(ok)})")
                    ax.legend(loc="upper right")
                    emit(fig, "throughput_timeline", lang)

    if engine_stats is None:  # regen 时从盘读引擎时间序列
        ej = out / "engine_stats.jsonl"
        if ej.exists():
            engine_stats = [json.loads(x) for x in ej.read_text("utf-8").splitlines() if x.strip()]
    if engine_stats:
        et = [s["t"] for s in engine_stats]
        kv = [s["kv"] * 100 for s in engine_stats]
        run_, wait_ = [s["running"] for s in engine_stats], [s["waiting"] for s in engine_stats]
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):  # KV cache 利用率随时间
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.plot(et, kv)
                ax.fill_between(et, kv, alpha=0.18)
                ax.set_ylim(0, max(100, max(kv) if kv else 100))
                ax.set_xlabel(_T["time_s"][lang])
                ax.set_ylabel(_T["kv_y"][lang])
                ax.set_title(_T["kv_title"][lang])
                emit(fig, "kv_timeline", lang)
            with _fig_ctx(plt, font):  # 并发(运行中 + 排队)随时间
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.plot(et, run_, label=_T["running"][lang])
                ax.plot(et, wait_, label=_T["waiting"][lang])
                ax.set_xlabel(_T["time_s"][lang])
                ax.set_ylabel(_T["conc_y"][lang])
                ax.set_title(_T["conc_title"][lang])
                ax.legend(loc="upper right")
                emit(fig, "concurrency_timeline", lang)
    return made


def regen_plots(run_dir, *, slo: Sequence[tuple[int, int]] | None = None) -> list[str]:
    """从 turns.jsonl 重画该 run 的图(en+zh);slo 缺省时取 run.json 已算的 goodput 阶梯。"""
    from types import SimpleNamespace

    p = Path(run_dir)
    tj = p / "turns.jsonl"
    if not tj.exists():
        return []
    if slo is None:  # 缺省取 run.json 已算的 goodput 阶梯(该 run 当初用的 SLO)
        rj = p / "run.json"
        gp = json.loads(rj.read_text(encoding="utf-8")).get("goodput", []) if rj.exists() else []
        slo = [(g["ttft_ms"], g["tpot_ms"]) for g in gp]
    rows = [json.loads(ln) for ln in tj.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return make_plots([SimpleNamespace(**r) for r in rows], p, slo=slo)


def compare_runs(
    runs_dir, out_dir=None, *, slo: Sequence[tuple[int, int]] | None = None
) -> list[str]:
    """跨 run 叠加对照图(TTFT / TPOT CDF),读各 run turns.jsonl + run.json 标签;en + zh。"""
    root = Path(runs_dir)
    out = Path(out_dir) if out_dir else root
    cjk = _resolve_cjk_font()
    runs = []
    for rj in sorted(root.glob("*/run.json")):
        tj = rj.parent / "turns.jsonl"
        if not tj.exists():
            continue
        try:
            meta = json.loads(rj.read_text(encoding="utf-8"))
            rows = [json.loads(s) for s in tj.read_text(encoding="utf-8").splitlines() if s.strip()]
        except (OSError, ValueError):
            continue
        runs.append((meta, [r for r in rows if r.get("ok")]))
    if len(runs) < 2:
        return []
    if slo is None:  # 缺省取首个 run 记录的 goodput 阶梯作 SLO 参考线
        slo = [(g["ttft_ms"], g["tpot_ms"]) for g in runs[0][0].get("goodput", [])]
    made: list[str] = []
    for attr, base, name, xlab, _log, _cdf in _METRICS[:2]:  # TTFT, TPOT
        slo_ms = _slo_ref(attr, slo)
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(6.4, 4))
                for meta, rows in runs:
                    xs = sorted(r[attr] for r in rows if r.get(attr, 0) > 0)
                    if not xs:
                        continue
                    ys = [(i + 1) / len(xs) for i in range(len(xs))]
                    lab = (
                        f"{meta['model']['name']}·{meta.get('tag', '')} "
                        f"(p50={fmt_ms(xs[len(xs) // 2])}, n={len(xs)})"
                    )
                    ax.plot(xs, ys, label=lab)
                if slo_ms:
                    _vline(ax, slo_ms, lang)
                ax.set_xscale("log")
                _apply_fmt(ax, xfn=_XFMT.get(attr))
                ax.set_xlabel(xlab[lang])
                ax.set_ylabel(_T["cdf_y"][lang])
                ax.set_title(f"{name[lang]} {_T['cdf'][lang]} — {_T['cmp'][lang]}")
                ax.legend(loc="lower right")
                fig.tight_layout()
                sub = out / "compare" / lang  # 对照图放 <runs>/compare/<lang>/
                sub.mkdir(parents=True, exist_ok=True)
                fig.savefig(sub / f"compare_{base}_cdf.png")
                plt.close(fig)
                made.append(f"compare/{lang}/compare_{base}_cdf.png")
    return made


def _win_span_min(window) -> float | None:
    """窗口跨度(分钟):'0:10'→10、'10'→10、None→None。"""
    if not window:
        return None
    try:
        s = str(window)
        if ":" in s:
            a, b = s.split(":", 1)
            return float(b) - float(a)
        return float(s)
    except ValueError:
        return None


_SWEEP_X = {  # x_key → (从 workload 取 x, 词表键)
    "rate": (lambda w: w.get("rate_per_min"), "rate_x"),
    "total": (lambda w: w.get("total_requests") or w.get("n_requests"), "total_x"),
    "window": (lambda w: _win_span_min(w.get("window")), "win_x"),
}
_TIER_KEYS = ["gp_strict", "gp_medium", "gp_loose"]


def sweep_curve(run_dirs, out_dir=None, *, x_key: str = "rate") -> list[str]:
    """负载扫描图:goodput(SLO 严/中/松)+ 原始输出吞吐随负载,右轴叠 SLO 达成率;en + zh。

    读各 run.json 的 workload/goodput/throughput;x_key ∈ {rate, total, window}。
    """
    getx, xlab_key = _SWEEP_X.get(x_key, _SWEEP_X["rate"])
    pts = []
    for d in run_dirs:
        rj = Path(d) / "run.json"
        if not rj.exists():
            continue
        try:
            r = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        x, gp = getx(r.get("workload", {})), r.get("goodput") or []
        if x is None or not gp:
            continue
        pts.append(
            {
                "x": x,
                "raw": r.get("throughput", {}).get("output_tok_s", 0.0),
                "good": [g.get("good_tok_s", 0.0) for g in gp],
                "attain": gp[0].get("attain", 0.0),
            }
        )
    if len(pts) < 2:
        return []
    pts.sort(key=lambda p: p["x"])
    xs = [p["x"] for p in pts]
    n_tiers = min(len(p["good"]) for p in pts)
    gstrict = [p["good"][0] for p in pts]
    kx = xs[gstrict.index(max(gstrict))]  # goodput 峰值(拐点)
    out = Path(out_dir) if out_dir else Path(run_dirs[0]).parent
    cjk = _resolve_cjk_font()
    made: list[str] = []
    for lang, font in _langs(cjk):
        with _fig_ctx(plt, font):
            fig, ax = plt.subplots(figsize=(6.4, 4))
            ax.plot(
                xs, [p["raw"] for p in pts], "--", color="#8c8c8c",
                marker="o", ms=4, label=_T["raw_tput"][lang],
            )
            for ti in range(min(n_tiers, len(_TIER_KEYS))):
                ax.plot(
                    xs, [p["good"][ti] for p in pts],
                    marker="o", ms=4, label=_T[_TIER_KEYS[ti]][lang],
                )
            ax.axvline(kx, ls=":", color="gray", lw=1)
            ax.text(
                kx, 0.02, f" {_T['knee'][lang]}", color="gray", fontsize=8,
                transform=ax.get_xaxis_transform(), va="bottom",
            )
            ax.set_xlabel(_T[xlab_key][lang])
            ax.set_ylabel(_T["tps_y"][lang])
            ax.set_xticks(xs)
            ax.set_ylim(bottom=0)
            ax.set_title(_T["sweep_title"][lang])
            ax.legend(loc="upper left")
            ax2 = ax.twinx()  # 右轴:最严档 SLO 达成率(%)
            ax2.grid(False)
            ax2.spines["top"].set_visible(False)
            ax2.plot(
                xs, [100 * p["attain"] for p in pts], color="#6a4c93",
                lw=1.2, ls="-.", marker="s", ms=3, alpha=0.8,
            )
            ax2.set_ylabel(_T["attain_y"][lang], color="#6a4c93")
            ax2.tick_params(axis="y", labelcolor="#6a4c93")
            ax2.set_ylim(0, 100)
            fig.tight_layout()
            sub = out / "compare" / lang
            sub.mkdir(parents=True, exist_ok=True)
            fig.savefig(sub / f"sweep_{x_key}.png")
            plt.close(fig)
            made.append(f"compare/{lang}/sweep_{x_key}.png")
    return made
