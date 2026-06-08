# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""回放负载构造:数据加载、时间窗截取、会话级下采样、时间调度、goodput 标尺。

由 replay 入口与 engine 执行层调用。
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from pathlib import Path

from datasets import load_dataset

from foretoken.bench.core.types import TurnResult


def next_send_ms(turn_idx: int, t_cur: int, t_prev: int, complete_prev_ms: float, t0: int) -> float:
    """该轮发出的相对(对 t0)时刻 ms。首轮 = 绝对;上一轮按时完成 → 绝对,超时 → 完成 + 原间隔。"""
    t_cur_rel = t_cur - t0
    if turn_idx == 0 or complete_prev_ms <= t_cur_rel:
        return float(t_cur_rel)
    return complete_prev_ms + (t_cur - t_prev)  # 超时:完成时刻 + 原本的思考间隔


def group_sessions(
    rows: Iterable[dict], *, window: tuple[int, int] | None = None
) -> dict[int, list[dict]]:
    """按 session_id 分组(内按 turn 排序);window=(a,b) ms:首轮在窗内才纳入,后续轮真实 ts>b 截断。"""
    sess: dict[int, list[dict]] = {}
    for r in rows:
        sess.setdefault(r["session_id"], []).append(r)
    for s in sess.values():
        s.sort(key=lambda r: r["turn"])
    if window is None:
        return sess
    t0 = min(r["timestamp_ms"] for s in sess.values() for r in s)
    a, b = window
    out: dict[int, list[dict]] = {}
    for k, s in sess.items():
        if not (a <= s[0]["timestamp_ms"] - t0 <= b):  # 首轮不在窗内 → 整会话不纳入
            continue
        out[k] = [r for r in s if r["timestamp_ms"] - t0 <= b]  # 截断真实 ts 超 b 的后续轮
    return out


def sample_sessions(
    sessions: dict[int, list[dict]],
    *,
    n_requests: int | None = None,
    fraction: float | None = None,
    seed: int = 0,
) -> dict[int, list[dict]]:
    """会话级下采样,**整会话保留**(多轮完整,绝不拆会话);把集群级 trace 负载匹配单实例。

    二选一:`n_requests` 随机选整会话直到累计轮(request)数达目标(整会话保留,末个会话可能略超);
    `fraction` 留该比例会话。都不给 → 原样。真实时间戳 / 突发形态不变,可复现(seed)。
    """
    if n_requests is None and fraction is None:
        return sessions
    sids = sorted(sessions)
    random.Random(seed).shuffle(sids)  # 可复现的随机序
    if n_requests is not None:
        kept: list[int] = []
        total = 0
        for sid in sids:
            if total >= n_requests:
                break
            kept.append(sid)
            total += len(sessions[sid])
        keep = set(kept)
    else:
        if not 0.0 < fraction <= 1.0:
            raise ValueError("fraction must be in (0, 1]")
        keep = set(sids[: max(1, round(len(sids) * fraction))])
    return {sid: s for sid, s in sessions.items() if sid in keep}  # 保原 session 顺序


def load_rows(dataset: str, split: str | None = None) -> list[dict]:
    """加载回放数据行(schema:session_id/turn/timestamp_ms/user;可选 assistant/system)。

    `dataset`:`.jsonl`/`.parquet`/`.csv` 文件、HF 数据集目录,或 HF hub id(配 `split`)。
    """
    p = Path(dataset)
    if p.is_file():  # 文件:按扩展名选 builder
        ext = p.suffix.lstrip(".")
        fmt = {"jsonl": "json", "json": "json", "parquet": "parquet", "csv": "csv"}.get(ext, ext)
        return list(load_dataset(fmt, data_files=str(p), split="train"))
    if p.is_dir():  # 本机 HF 数据集目录
        return list(load_dataset(str(p), name=split, split="train"))
    return list(load_dataset(dataset, split, split="train"))  # HF hub id;split=配置名


def parse_window(spec: str | None) -> tuple[int, int] | None:
    """'N' → (0, N 分);'A:B' → (A 分, B 分);None → 全量。单位 ms。"""
    if not spec:
        return None
    if ":" in spec:
        a, b = spec.split(":", 1)
        return (int(float(a) * 60_000), int(float(b) * 60_000))
    return (0, int(float(spec) * 60_000))


def deadline_seconds(
    window: tuple[int, int] | None, sec_multiplier: float, tail_factor: float
) -> float | None:
    """回放墙钟上限 s = 窗口跨度 × sec_multiplier × tail_factor;到点取消在飞请求(掐长尾)。

    无窗口或 tail_factor<=0 → None(不设限,跑到全部完成)。
    """
    if not window or tail_factor <= 0:
        return None
    return (window[1] - window[0]) / 1000.0 * sec_multiplier * tail_factor


def goodput_per_gpu_byte_second(
    results: Iterable[TurnResult],
    *,
    ttft_ms: float,
    tpot_ms: float,
    duration_s: float,
    gpu_bytes: float,
) -> float:
    """满足 SLO(TTFT 且 TPOT)的有效 output token / (时长 × GPU 字节)——统一标尺。"""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if gpu_bytes <= 0:
        raise ValueError("gpu_bytes must be > 0")
    good = sum(
        r.output_tokens for r in results if r.ok and r.ttft_ms <= ttft_ms and r.tpot_ms <= tpot_ms
    )
    return good / (duration_s * gpu_bytes)
