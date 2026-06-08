"""出图(matplotlib,可选):每指标 CDF + 直方图、输出长度分布、TTFT 随到达时刻散点,跨 run 对照。

全部从 saved per-turn 数据(turns.jsonl)生成,en + zh 双份(`<run>/en/`、`<run>/zh/`);
无 CJK 字体只出 en,无 matplotlib 跳过。SLO 参考线取 `slo` 阶梯最严档。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from .metrics import DEFAULT_SLO, fmt_ms

# 已注册字体里按序挑中文字体(服务器 mplfonts 装的 Noto / 本地 Windows 雅黑等)
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
    "cmp": {"en": "comparison", "zh": "对照"},
    "time_s": {"en": "time (s)", "zh": "时刻 (s)"},
    "kv_y": {"en": "KV cache usage (%)", "zh": "KV 利用率 (%)"},
    "kv_title": {"en": "KV cache usage over time", "zh": "KV 利用率随时间"},
    "conc_y": {"en": "requests", "zh": "请求数"},
    "conc_title": {"en": "concurrency over time", "zh": "并发随时间"},
    "running": {"en": "running", "zh": "在飞"},
    "waiting": {"en": "waiting", "zh": "排队"},
}


def _throughput_series(ok, n_bins: int = 80):
    """输出吞吐随时间:每轮 output_tokens 按 decode 区间 [首token, 完成] 均摊到时间桶 → tok/s。

    需 send_ms(旧 run 无则返回 None)。返回 (桶中心时刻 s, 每桶 tok/s)。
    """
    iv = []
    for r in ok:
        s0 = getattr(r, "send_ms", 0)
        if not s0:
            continue
        s = (s0 + r.ttft_ms) / 1000.0
        e = getattr(r, "complete_ms", 0) / 1000.0
        if e > s and r.output_tokens > 0:
            iv.append((s, e, r.output_tokens))
    if not iv:
        return None
    t0 = min(s for s, _, _ in iv)
    t1 = max(e for _, e, _ in iv)
    if t1 <= t0:
        return None
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
    """大数缩写:1000→1k、1.5e6→1.5M;小数普通(避免 100000 这种僵化大数)。"""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:g}M"
    if a >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def _apply_fmt(ax, xfn=None, yfn=None):
    """给指定轴设领域格式器(log 轴尤其需要:否则 decade 标成僵化大数);minor 不标。"""
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
    results, out: Path, *, slo: Sequence[tuple[int, int]] = DEFAULT_SLO, engine_stats=None
) -> list[str]:
    """每指标 CDF + 直方图、E2E、输入/输出长度、时间线、吞吐、引擎 KV%/并发随时间;en + zh。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return []
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

    series = _throughput_series(ok)
    if series:
        xs_t, rate = series
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.plot(xs_t, rate)
                ax.fill_between(xs_t, rate, alpha=0.18)
                _apply_fmt(ax, yfn=_fmt_k)
                ax.set_xlabel(_T["tput_x"][lang])
                ax.set_ylabel(_T["tput_y"][lang])
                ax.set_title(f"{_T['tput'][lang]} (n={len(ok)})")
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
            with _fig_ctx(plt, font):  # 并发(在飞 + 排队)随时间
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
    if slo is None:
        rj = p / "run.json"
        gp = json.loads(rj.read_text(encoding="utf-8")).get("goodput", []) if rj.exists() else []
        slo = [(g["ttft_ms"], g["tpot_ms"]) for g in gp] or DEFAULT_SLO
    rows = [json.loads(ln) for ln in tj.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return make_plots([SimpleNamespace(**r) for r in rows], p, slo=slo)


def compare_runs(
    runs_dir, out_dir=None, *, slo: Sequence[tuple[int, int]] = DEFAULT_SLO
) -> list[str]:
    """跨 run 叠加对照图(TTFT / TPOT CDF),读各 run turns.jsonl + run.json 标签;en + zh。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
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
