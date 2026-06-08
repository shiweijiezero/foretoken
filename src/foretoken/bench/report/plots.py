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
    "cmp": {"en": "comparison", "zh": "对照"},
}
# 指标:attr, 文件前缀, 名{en,zh}, x 轴{en,zh}, log_x, 出 CDF?(SLO 参考线由 _slo_ref 从 slo 取)
_METRICS = [
    ("ttft_ms", "ttft", {"en": "TTFT", "zh": "TTFT"},
     {"en": "TTFT (ms)", "zh": "首 token 延迟 TTFT (ms)"}, True, True),
    ("tpot_ms", "tpot", {"en": "TPOT", "zh": "TPOT"},
     {"en": "TPOT (ms)", "zh": "每 token 延迟 TPOT (ms)"}, True, True),
    ("output_tokens", "tokens", {"en": "output length", "zh": "输出长度"},
     {"en": "output tokens", "zh": "输出 token 数"}, True, False),
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


def _fig_ctx(plt, font):
    rc = {"axes.unicode_minus": False}
    if font:
        rc["font.sans-serif"] = [font]
    return plt.rc_context(rc)


def _vline(ax, x, lang):
    """竖直 SLO 参考线 + 标注(y 用 axes 比例,CDF / 直方图通用)。"""
    ax.axvline(x, ls="--", color="gray", lw=1)
    ax.text(
        x, 0.96, f" {fmt_ms(x)} {_T['slo'][lang]}", color="gray", fontsize=8,
        transform=ax.get_xaxis_transform(), va="top",
    )


def make_plots(results, out: Path, *, slo: Sequence[tuple[int, int]] = DEFAULT_SLO) -> list[str]:
    """每指标出 CDF + 直方图(延迟带 SLO 线),外加输出长度分布 + TTFT 随到达时刻散点;en + zh。"""
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
        fig.savefig(out / lang / f"{base}.png", dpi=110)
        plt.close(fig)
        made.append(f"{lang}/{base}.png")

    for attr, base, name, xlab, log_x, do_cdf in _METRICS:
        xs = sorted(v for v in (getattr(r, attr, 0) for r in ok) if v and v > 0)
        if not xs:
            continue
        slo_ms = _slo_ref(attr, slo)
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
                    ax.set_xlabel(xlab[lang])
                    ax.set_ylabel(_T["cdf_y"][lang])
                    ax.set_title(f"{name[lang]} {_T['cdf'][lang]} (n={len(xs)})")
                    ax.grid(True, alpha=0.3)
                    emit(fig, f"{base}_cdf", lang)
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.hist(xs, bins=bins, color="#4c78a8", edgecolor="white", linewidth=0.3)
                if slo_ms:
                    _vline(ax, slo_ms, lang)
                if log_x:
                    ax.set_xscale("log")
                ax.set_xlabel(xlab[lang])
                ax.set_ylabel(_T["hist_y"][lang])
                ax.set_title(f"{name[lang]} {_T['hist'][lang]} (n={len(xs)})")
                ax.grid(True, alpha=0.3, axis="y")
                emit(fig, f"{base}_hist", lang)

    ref = _slo_ref("ttft_ms", slo)
    pts = [(r.send_ms / 1000.0, r.ttft_ms) for r in ok if getattr(r, "send_ms", 0)]
    if pts:
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=8, alpha=0.5)
                if ref:
                    ax.axhline(ref, ls="--", color="gray", lw=1)
                ax.set_yscale("log")
                ax.set_xlabel(_T["time_x"][lang])
                ax.set_ylabel("TTFT (ms)")
                ax.set_title(f"{_T['timeline'][lang]} (n={len(pts)})")
                ax.grid(True, alpha=0.3)
                emit(fig, "ttft_timeline", lang)
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
                ax.set_xlabel(xlab[lang])
                ax.set_ylabel(_T["cdf_y"][lang])
                ax.set_title(f"{name[lang]} {_T['cdf'][lang]} — {_T['cmp'][lang]}")
                ax.grid(True, alpha=0.3)
                ax.legend(fontsize=8, loc="lower right")
                fig.tight_layout()
                (out / lang).mkdir(exist_ok=True)
                fig.savefig(out / lang / f"compare_{base}_cdf.png", dpi=120)
                plt.close(fig)
                made.append(f"{lang}/compare_{base}_cdf.png")
    return made
