# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist completed request measurements before aggregate publication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_request_records(directory: Path, name: str = "raw_output") -> list[dict[str, Any]]:
    """Read published requests or their flushed journal for offline result consumers."""
    published = directory / f"{name}.json"
    if published.is_file():
        return json.loads(published.read_text(encoding="utf-8"))
    journal = directory / f"{name}.jsonl"
    if journal.is_file():
        with journal.open(encoding="utf-8") as file:
            return sorted((json.loads(line) for line in file if line.strip()), key=lambda row: row["start_time"])
    return []


class RequestRecordWriter:
    """Own a flushed JSONL journal so completed requests survive an interrupted run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = path.open("w", encoding="utf-8", buffering=1)

    def write(self, record: dict[str, Any]) -> None:
        """Append a completed request without retaining request or response content."""
        self.file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.file.flush()

    def close(self) -> None:
        """Close the journal after the owning result lifecycle finishes publication."""
        self.file.close()
