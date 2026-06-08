# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""流式生成计时。"""

from __future__ import annotations

import time


class StreamTimer:
    """流式生成计时:首 token 处 `mark_first()`,结束 `metrics(n)` → (ttft, tpot, e2e) ms。"""

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.first: float | None = None

    def mark_first(self) -> None:
        if self.first is None:
            self.first = time.perf_counter()

    def metrics(self, n_tokens: int) -> tuple[float, float, float]:
        """→ (ttft_ms, tpot_ms, e2e_ms);TPOT 按 (end−first)/(n−1),n≤1 记 0。"""
        end = time.perf_counter()
        e2e = (end - self.start) * 1000.0
        if self.first is None:
            return 0.0, 0.0, e2e
        ttft = (self.first - self.start) * 1000.0
        tpot = (end - self.first) * 1000.0 / (n_tokens - 1) if n_tokens > 1 else 0.0
        return ttft, tpot, e2e
