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
        _row("TTFT", _fmt_ms(t["p50"]), _fmt_ms(t["p90"]), _fmt_ms(t["p99"])),
        _row("TPOT", _fmt_ms(p["p50"]), _fmt_ms(p["p90"]), _fmt_ms(p["p99"])),
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
                f"{_fmt_ms(g['ttft_ms'])}, {g['tpot_ms']}ms",
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


# 全部图从 saved per-turn 数据(turns.jsonl)生成,en + zh 双份;无 CJK 字体只出 en,无 matplotlib 跳过。
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
# 指标:attr, 文件前缀, 名{en,zh}, x 轴{en,zh}, strict SLO ms|None, log_x, 出 CDF?
_METRICS = [
    ("ttft_ms", "ttft", {"en": "TTFT", "zh": "TTFT"},
     {"en": "TTFT (ms)", "zh": "首 token 延迟 TTFT (ms)"}, 2000, True, True),
    ("tpot_ms", "tpot", {"en": "TPOT", "zh": "TPOT"},
     {"en": "TPOT (ms)", "zh": "每 token 延迟 TPOT (ms)"}, 80, True, True),
    ("output_tokens", "tokens", {"en": "output length", "zh": "输出长度"},
     {"en": "output tokens", "zh": "输出 token 数"}, None, True, False),
]


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
        x, 0.96, f" {_fmt_ms(x)} {_T['slo'][lang]}", color="gray", fontsize=8,
        transform=ax.get_xaxis_transform(), va="top",
    )


def _plots(results, out: Path) -> list[str]:
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

    for attr, base, name, xlab, slo_ms, log_x, do_cdf in _METRICS:
        xs = sorted(v for v in (getattr(r, attr, 0) for r in ok) if v and v > 0)
        if not xs:
            continue
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

    pts = [(r.send_ms / 1000.0, r.ttft_ms) for r in ok if getattr(r, "send_ms", 0)]
    if pts:
        for lang, font in _langs(cjk):
            with _fig_ctx(plt, font):
                fig, ax = plt.subplots(figsize=(5, 3.2))
                ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=8, alpha=0.5)
                ax.axhline(2000, ls="--", color="gray", lw=1)
                ax.set_yscale("log")
                ax.set_xlabel(_T["time_x"][lang])
                ax.set_ylabel("TTFT (ms)")
                ax.set_title(f"{_T['timeline'][lang]} (n={len(pts)})")
                ax.grid(True, alpha=0.3)
                emit(fig, "ttft_timeline", lang)
    return made


def compare_runs(runs_dir, out_dir=None) -> list[str]:
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
    for attr, base, name, xlab, slo_ms, _log, _cdf in _METRICS[:2]:  # TTFT, TPOT
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
                        f"(p50={_fmt_ms(xs[len(xs) // 2])}, n={len(xs)})"
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


def regen_plots(run_dir) -> list[str]:
    """从 turns.jsonl 重画该 run 的图(en+zh);给旧 run 补图 / 字体变更后重出。"""
    from types import SimpleNamespace

    p = Path(run_dir)
    tj = p / "turns.jsonl"
    if not tj.exists():
        return []
    rows = [json.loads(ln) for ln in tj.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return _plots([SimpleNamespace(**r) for r in rows], p)


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


if __name__ == "__main__":  # python -m foretoken.bench.report [runs_dir] [--plots] [--compare]
    import sys

    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    d = argv[0] if argv else "runs"
    if "--plots" in sys.argv:  # 给所有 run 重画图(en+zh)
        for tj in sorted(Path(d).glob("*/turns.jsonl")):
            regen_plots(tj.parent)
        print(f"已重画各 run 图(en+zh):{d}")
    if "--compare" in sys.argv:  # 跨 run 对照图(en+zh)
        print(f"对照图:{compare_runs(d)}")
    rebuild_index(d)
    print(f"INDEX 重建:{d}/INDEX.md")
