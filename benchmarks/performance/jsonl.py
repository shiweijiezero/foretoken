# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read line-delimited JSON used by benchmark workloads and parameter sweeps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def load_jsonl(
    path: Path | str, *, allow_comments: bool = False
) -> Iterator[tuple[int, Any]]:
    """Yield non-empty JSON values and one-based line numbers."""
    jsonl_path = Path(path)
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"JSONL not found: {jsonl_path}")
    with jsonl_path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            line = line.strip()
            if not line or (allow_comments and line.startswith("#")):
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON at {jsonl_path}:{line_no}: {error}"
                ) from error
