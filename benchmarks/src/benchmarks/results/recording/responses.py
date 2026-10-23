# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Write optional request and assembled response evidence as requests finish."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


class ResponseWriter:
    """Own separate JSONL files for warmup and measured response content."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.files: dict[str, TextIO] = {}

    def write(self, phase: str, record: dict[str, Any]) -> None:
        """Persist one completed or interrupted request without retaining other responses."""
        if phase not in self.files:
            name = "warmup_responses.jsonl" if phase == "warmup" else "responses.jsonl"
            self.files[phase] = (self.directory / name).open("w", encoding="utf-8")
        file = self.files[phase]
        file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        file.flush()

    def close(self) -> None:
        """Close every opened response file at the owning result lifecycle's exit."""
        for file in self.files.values():
            file.close()
        self.files.clear()
