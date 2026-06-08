# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""回放的逐轮结果类型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TurnResult:
    session_id: int
    turn: int
    ttft_ms: float
    tpot_ms: float
    e2e_ms: float
    output_tokens: int
    prompt_tokens: int = 0  # 输入 token 数(引擎统计;供总吞吐 / prompt 长度分布)
    send_ms: float = 0.0  # 实际发出时刻(相对回放起点 ms;供时间线 / 并发图,从 turns.jsonl 可重现)
    complete_ms: float = 0.0  # 完成时刻(相对回放起点 ms)
    text: str = ""  # 现场生成的回复(写 cases,不入 turns.jsonl)
    user: str = ""  # 该轮用户输入(写 cases)
    system: str | None = None  # 系统提示(仅 turn 0;写 cases)
    ok: bool = True
