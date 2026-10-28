# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Write optional request and assembled response evidence as requests finish."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TextIO


class ResponseWriter:
    """Own separate JSONL files for warmup and measured response content."""

    def __init__(self, directory: str, *, filename: str = "responses.jsonl", append: bool = False) -> None:
        """Own response files, optionally appending to evidence restored by an evaluation."""
        self.directory = Path(directory)
        self.filename = filename
        self.append = append
        self.files: dict[str, TextIO] = {}
        self._resources = ExitStack()
        self._failure: BaseException | None = None

    def write(self, phase: str, record: dict[str, Any]) -> None:
        """Persist one completed or interrupted request without retaining other responses."""
        if self._failure is not None:
            raise self._failure
        try:
            if phase not in self.files:
                name = f"warmup_{self.filename}" if phase == "warmup" else self.filename
                self.files[phase] = self._resources.enter_context(
                    (self.directory / name).open("a" if self.append else "w", encoding="utf-8")
                )
            file = self.files[phase]
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.flush()
        except BaseException as error:
            # A retry must not append another record behind a partially written JSON line.
            self._failure = error
            raise

    def close(self) -> None:
        """Close all response files and propagate any retained write failure to the owner."""
        try:
            self._resources.close()
        finally:
            self.files.clear()
        if self._failure is not None:
            raise self._failure
