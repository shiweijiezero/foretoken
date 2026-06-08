# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""计价成本报告:按 GPU 时价把各配置折算成单位 token 成本,横向对比。

token 工厂的核心经济指标是每百万 token 的成本。原始口径(`$/Mtok`)只看产能;
达成口径(`$/Mtok@SLO`)只计满足延迟 SLO、即真正可交付的 token —— 后者才是定价依据。
默认时价取参考云上按需价(USD/h);自建 / 沐曦集群成本不同,用 `--gpu-rate` 覆盖。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt

from .compare import _bars, _gp, _load
from .plots import _apply_fmt, _fig_ctx, _langs, _resolve_cjk_font

# 参考云上按需价(USD / GPU·h);按 GPU 名子串匹配,--gpu-rate 覆盖。
DEFAULT_RATES = {
    "A100": 1.8, "H100": 3.0, "H200": 3.5, "L40": 1.0, "L20": 0.8,
    "4090": 0.4, "A10": 0.75, "V100": 0.8,
}
_FALLBACK_RATE = 2.0

_T = {
    "title": {"en": "cost per 1M tokens by config", "zh": "各配置每百万 token 成本"},
    "y": {"en": "USD / 1M tokens", "zh": "美元 / 百万 token"},
    "raw": {"en": "raw", "zh": "原始"},
    "good": {"en": "SLO-met", "zh": "达成 SLO"},
}


def _rate(gpu_name: str, rates: dict[str, float]) -> float:
    """按 GPU 名子串匹配时价;无匹配回退默认。"""
    for key, val in rates.items():
        if key.upper() in (gpu_name or "").upper():
            return val
    return _FALLBACK_RATE


def cost_rows(loaded, *, rates: dict[str, float], slo_tier: int = 0) -> list[dict]:
    """逐 run 折算成本:GPU·h、运行成本、$/Mtok(原始 / 达成)、tok/$。按 $/Mtok@SLO 升序。"""
    rows: list[dict] = []
    for lbl, run, _ in loaded:
        gpu = run.get("gpu", {})
        n = gpu.get("count") or 0
        dur_s = run.get("duration_s") or 0.0
        rate = _rate(gpu.get("name", ""), rates)
        gpu_h = n * dur_s / 3600.0
        cost = gpu_h * rate
        raw_tok = (run.get("output_len") or {}).get("total", 0)
        tier = _gp(run)[slo_tier] if slo_tier < len(_gp(run)) else {}
        good_tok = (tier.get("good_tok_s", 0) or 0) * dur_s
        rows.append(
            {
                "label": lbl,
                "gpus": f"{n}×{_short_gpu(gpu.get('name', '?'))}",
                "rate": rate,
                "dur_s": dur_s,
                "cost": cost,
                "raw_tok_s": (run.get("throughput") or {}).get("output_tok_s", 0.0),
                "usd_per_mtok_raw": (cost / (raw_tok / 1e6)) if raw_tok else None,
                "attain": tier.get("attain", 0.0),
                "good_tok_s": tier.get("good_tok_s", 0.0),
                "usd_per_mtok_good": (cost / (good_tok / 1e6)) if good_tok else None,
                "tok_per_usd": (raw_tok / cost) if cost else None,
            }
        )
    rows.sort(key=lambda r: (r["usd_per_mtok_good"] is None, r["usd_per_mtok_good"] or 0.0))
    return rows


def _short_gpu(name: str) -> str:
    """GPU 名取关键型号(A100 / 4090 / H100 …)。"""
    for k in (*DEFAULT_RATES, "RTX"):
        if k.upper() in name.upper():
            return k
    return name.split()[-1] if name else "?"


def _usd(v) -> str:
    return f"${v:,.2f}" if isinstance(v, int | float) else "-"


def cost_table(rows: list[dict], slo: tuple[int, int] | None) -> str:
    """成本对比表(markdown);按可交付 token 成本排序,最便宜在前。"""
    tier = f"@SLO {slo[0]}/{slo[1]}ms" if slo else "@SLO"
    head = [
        "run", "GPU", "$/GPU·h", "时长", "out tok/s", "$/Mtok(原始)",
        f"达成{tier}", "good tok/s", f"**$/Mtok({tier})**", "tok/$",
    ]
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows:
        cells = [
            r["label"], r["gpus"], f"${r['rate']:.2f}", f"{r['dur_s'] / 60:.0f}min",
            f"{r['raw_tok_s']:.0f}",
            _usd(r["usd_per_mtok_raw"]),
            f"{r['attain'] * 100:.0f}%", f"{r['good_tok_s']:.0f}",
            _usd(r["usd_per_mtok_good"]),
            f"{r['tok_per_usd']:,.0f}" if r["tok_per_usd"] else "-",
        ]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def _cost_plot(rows: list[dict], out: Path) -> list[str]:
    """双语柱图:各配置 $/Mtok(原始 vs 达成 SLO)。"""
    cjk = _resolve_cjk_font()
    made: list[str] = []
    labels = [r["label"] for r in rows]
    raw = [r["usd_per_mtok_raw"] or 0 for r in rows]
    good = [r["usd_per_mtok_good"] or 0 for r in rows]
    for lang, font in _langs(cjk):
        with _fig_ctx(plt, font):
            fig, ax = plt.subplots(figsize=(7.2, 4))
            _bars(ax, labels, [(_T["raw"][lang], raw), (_T["good"][lang], good)])
            _apply_fmt(ax, yfn=lambda v: f"${v:.1f}")
            ax.set_ylabel(_T["y"][lang])
            ax.set_title(_T["title"][lang])
            ax.legend(loc="upper left")
            ax.tick_params(axis="x", labelrotation=20)
            fig.tight_layout()
            sub = out / lang
            sub.mkdir(parents=True, exist_ok=True)
            fig.savefig(sub / "cost_per_mtok.png")
            plt.close(fig)
            made.append(f"{lang}/cost_per_mtok.png")
    return made


def cost_report(
    run_dirs, *, out_dir=None, rates: dict[str, float] | None = None, slo_tier: int = 0
) -> Path | None:
    """各配置计价成本报告 → 写 <out>/cost.md(成本表 + 柱图);返回 out 目录。"""
    dirs = list(run_dirs)
    if len(dirs) == 1 and not (Path(dirs[0]) / "run.json").exists():
        dirs = sorted(str(p.parent) for p in Path(dirs[0]).glob("*/run.json"))
    loaded = _load(dirs)
    if not loaded:
        return None
    rates = {**DEFAULT_RATES, **(rates or {})}
    rows = cost_rows(loaded, rates=rates, slo_tier=slo_tier)
    slo = None
    for _, run, _ in loaded:  # 取首个 run 的对应档位 SLO 阈值用于表头
        tiers = _gp(run)
        if slo_tier < len(tiers) and tiers[slo_tier]:
            slo = (tiers[slo_tier].get("ttft_ms"), tiers[slo_tier].get("tpot_ms"))
            break
    out = Path(out_dir) if out_dir else Path("results/compare") / "cost"
    out.mkdir(parents=True, exist_ok=True)
    plots = _cost_plot(rows, out)
    img = next((p for p in plots if p.startswith("zh")), plots[0] if plots else None)
    md = [
        f"# 计价成本报告({len(rows)} 组)",
        "",
        "时价为参考云上按需价(USD / GPU·h);自建 / 沐曦集群成本不同,以 `--gpu-rate` 覆盖。",
        "原始口径只看产能;达成口径只计满足 SLO、即可交付的 token,是定价依据。",
        "",
        cost_table(rows, slo),
    ]
    if img:
        md += ["## 每百万 token 成本", f"![{img}]({img})", ""]
    (out / "cost.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out
