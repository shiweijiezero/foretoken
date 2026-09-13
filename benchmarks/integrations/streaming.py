# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Measure Chat Completions streaming chunks using vLLM benchmark semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatStreamTiming:
    """Track chunks with non-empty choices for one request attempt.

    Role, tool-call, and finish chunks count when choices is non-empty.
    Usage-only chunks do not. Intervals describe received chunks, not tokens.
    """

    first_output_at: float | None = None
    last_output_at: float | None = None
    intervals: list[float] = field(default_factory=list)

    def observe(self, response: dict[str, Any], received_at: float) -> None:
        """Record one decoded streaming chunk when its choices list is non-empty."""
        if not response.get("choices"):
            return
        if self.first_output_at is None:
            self.first_output_at = received_at
        if self.last_output_at is not None:
            self.intervals.append(received_at - self.last_output_at)
        self.last_output_at = received_at
