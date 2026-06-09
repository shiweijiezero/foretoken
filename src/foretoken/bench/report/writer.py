# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""单 run 写盘组装:turns.jsonl / cases / engine_stats / run.json / summary.md / 图。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .markdown import render_summary
from .metrics import summarize
from .plots import make_plots

_HEAVY = {"text", "user", "system"}  # 不入 turns.jsonl(指标表)的重字段;另存 cases


def _fence(text: str) -> str:
    """代码围栏:长度 = 内容里最长 backtick 连串 + 1(≥3),保证正文原样显示、不被当 markdown/LaTeX。"""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _cases_md(rows, max_md_sessions: int = 5) -> str:
    """渲染可读 cases.md:角色加粗 + 正文置于代码围栏(对话原文含 $ / | / # 等不被解析)。"""
    by_sid: dict = {}
    for d in rows:
        by_sid.setdefault(d["session_id"], []).append(d)
    md = [f"# 样例输入输出(前 {max_md_sessions} 个会话)"]
    for sid in list(by_sid)[:max_md_sessions]:
        md.append(f"\n## session {sid}")
        for d in sorted(by_sid[sid], key=lambda x: x["turn"]):
            for role in ("system", "user", "assistant"):  # system 仅首轮非空
                t = d.get(role)
                if not t:
                    continue
                fc = _fence(t)
                md.append(f"\n**{role}**\n\n{fc}\n{t}\n{fc}")
    return "\n".join(md) + "\n"


def _write_cases(results, out: Path, *, mode: str = "sample", max_md_sessions: int = 5) -> None:
    """写每轮输入输出:off 不写 / sample 仅 cases.md(前若干会话,可读)/ full 加 cases.jsonl(全量)。"""
    if mode == "off":
        return
    rows = [
        {k: d[k] for k in ("session_id", "turn", "system", "user", "ok")} | {"assistant": d["text"]}
        for d in (asdict(r) for r in results)
    ]
    if mode == "full":  # 全量(JSONL,大文件友好 / 可 grep)
        with (out / "cases.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "cases.md").write_text(_cases_md(rows, max_md_sessions), encoding="utf-8")


def regen_cases(run_dir, max_md_sessions: int = 5) -> bool:
    """从 cases.jsonl 重建 cases.md(改了渲染后无需重跑 benchmark);无 cases.jsonl 返回 False。"""
    cj = Path(run_dir) / "cases.jsonl"
    if not cj.exists():
        return False
    rows = [json.loads(ln) for ln in cj.read_text(encoding="utf-8").splitlines() if ln.strip()]
    (Path(run_dir) / "cases.md").write_text(_cases_md(rows, max_md_sessions), encoding="utf-8")
    return True


def write_run(
    results,
    meta: dict,
    out_dir,
    *,
    slo: Sequence[tuple[int, int]],
    engine_stats: list[dict] | None = None,
    engine_requests: list[dict] | None = None,
    cases: str = "sample",
) -> dict:
    """写 turns.jsonl / cases / engine_stats / run.json / summary.md / 图;返回完整 run dict。

    cases ∈ {off, sample, full}:off 不存、sample 仅可读样例 cases.md、full 加全量 cases.jsonl。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "turns.jsonl").open("w", encoding="utf-8") as f:
        for r in results:  # 指标表:剔除重字段(输入输出另存 cases)
            f.write(json.dumps({k: v for k, v in asdict(r).items() if k not in _HEAVY}) + "\n")
    _write_cases(results, out, mode=cases)
    if engine_stats:
        with (out / "engine_stats.jsonl").open("w", encoding="utf-8") as f:
            for s in engine_stats:
                f.write(json.dumps(s) + "\n")
    if engine_requests:  # 完成请求的时间分解
        with (out / "engine_requests.jsonl").open("w", encoding="utf-8") as f:
            for r in engine_requests:
                f.write(json.dumps(r) + "\n")
    summary = summarize(
        results,
        duration_s=meta["duration_s"],
        gpu_bytes=meta["gpu"]["total_bytes"],
        num_gpus=meta["gpu"]["count"],
        slo=slo,
        gpu_name=meta["gpu"]["name"],
        engine_stats=engine_stats,
        engine_requests=engine_requests,
    )
    run = {**meta, **summary, "cases": cases}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["plots"] = make_plots(results, out, slo=slo, engine_stats=engine_stats)
    (out / "summary.md").write_text(render_summary(run), encoding="utf-8")
    return run
