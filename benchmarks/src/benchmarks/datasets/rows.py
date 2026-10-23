# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stream source rows from local files and Hugging Face without applying request policy."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from benchmarks.datasets.huggingface import (
    is_hf_dataset_spec,
    is_hf_file_uri,
    iter_hf_rows,
    resolve_hf_file_uri,
)


def iter_dataset_rows(
    source: str | Path, *, allow_comments: bool = False
) -> Iterator[tuple[Path, int, int, Any]]:
    """Yield normalized rows from a local JSONL/JSON file, Hub file, or Hub dataset."""
    selector = str(source)
    local_path = Path(selector).expanduser()
    if local_path.is_file():
        yield from _iter_dataset_file(local_path, allow_comments=allow_comments)
        return
    if is_hf_file_uri(selector):
        yield from _iter_dataset_file(
            Path(resolve_hf_file_uri(selector)), allow_comments=allow_comments
        )
        return
    if is_hf_dataset_spec(selector):
        dataset_label = Path(f"hf://{selector}")
        for row_index, row in iter_hf_rows(selector):
            yield dataset_label, row_index + 1, row_index, row
        return
    raise FileNotFoundError(f"Dataset not found: {local_path}")


def iter_jsonl_rows(
    path: Path | str, *, allow_comments: bool = False
) -> Iterator[tuple[Path, int, int, Any]]:
    """Yield non-empty JSONL values with their file path, line, and row indexes."""
    jsonl_path = Path(path).expanduser()
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"JSONL not found: {jsonl_path}")
    row_index = 0
    with jsonl_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line or (allow_comments and line.startswith("#")):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON at {jsonl_path}:{line_number}: {error}"
                ) from error
            yield jsonl_path, line_number, row_index, row
            row_index += 1


def _iter_dataset_file(path: Path, *, allow_comments: bool) -> Iterator[tuple[Path, int, int, Any]]:
    """Stream JSON-array rows or read JSONL records without materializing a large corpus."""
    if path.suffix != ".json":
        yield from iter_jsonl_rows(path, allow_comments=allow_comments)
        return
    with path.open("r", encoding="utf-8") as stream:
        first = stream.read(1)
        while first and first.isspace():
            first = stream.read(1)
    if first != "[":
        yield from iter_jsonl_rows(path, allow_comments=allow_comments)
        return
    import ijson

    with path.open("rb") as stream:
        for row_index, row in enumerate(ijson.items(stream, "item", use_float=True)):
            yield path, row_index + 1, row_index, row
