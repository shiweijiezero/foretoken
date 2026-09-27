# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execution contract shared by video quality evaluator adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class VideoEvaluationIdentity:
    """Describe a video run and its evaluator-specific provenance fields."""

    model: str
    evaluation_mode: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class VideoEvaluationCommand:
    """Describe a native evaluator process owned and logged by Foretoken."""

    arguments: tuple[str, ...]
    cwd: str
    environment: dict[str, str]


class VideoEvaluator(Protocol):
    """Supply run identity, native execution, and normalized video quality scores."""

    name: str

    def describe(self) -> VideoEvaluationIdentity:
        """Return run metadata before result destinations open."""
        ...

    def prepare(self, native_directory: Path) -> VideoEvaluationCommand:
        """Prepare native inputs and return the child process invocation."""
        ...

    def read_metrics(self, native_directory: Path) -> dict[str, Any]:
        """Read native reports into the shared score representation."""
        ...
