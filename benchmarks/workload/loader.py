# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Build request list from prompt or JSONL dataset path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from benchmarks.config import BenchConfig, DatasetConfig


def load_jsonl(path: Path | str) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_no, parsed_object)`` for non-empty JSONL lines."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"JSONL not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at {p}:{line_no}: {e}") from e


def _normalize(obj: Any, path: Path, line_no: int) -> dict[str, Any]:
    if isinstance(obj, list):
        if not obj:
            raise ValueError(f"Empty messages list at {path}:{line_no}")
        return {"messages": obj}

    if not isinstance(obj, dict):
        raise ValueError(f"Expected object or messages list at {path}:{line_no}")

    if "messages" in obj:
        messages = obj["messages"]
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Invalid messages at {path}:{line_no}")
        request: dict[str, Any] = {"messages": messages}
        if obj.get("tools"):
            request["tools"] = obj["tools"]
        return request

    if "prompt" in obj:
        request = {"prompt": str(obj["prompt"])}
        if obj.get("tools"):
            request["tools"] = obj["tools"]
        return request

    raise ValueError(
        f"Line must be messages list or contain 'messages'/'prompt' "
        f"at {path}:{line_no}"
    )


def load_requests(config: BenchConfig) -> list[dict[str, Any]]:
    """Load requests for a single run from ``--prompt`` or ``--dataset-path``."""
    ds: DatasetConfig = config.dataset
    number = config.load.primary_number

    if ds.prompt:
        return [{"prompt": ds.prompt} for _ in range(number)]

    path = ds.primary_dataset_path
    if not path:
        raise ValueError(
            "No workload source. Pass --prompt or --dataset-path "
            "(JSONL with messages/prompt per line)."
        )

    p = Path(path)
    offset = max(int(ds.dataset_offset), 0)
    requests: list[dict[str, Any]] = []
    for line_no, obj in load_jsonl(p):
        if line_no <= offset:
            continue
        requests.append(_normalize(obj, p, line_no))
        if len(requests) >= number:
            break

    if not requests:
        raise ValueError(f"No requests loaded from {p} (offset={offset})")

    # Repeat if file shorter than requested number.
    if len(requests) < number:
        base = list(requests)
        while len(requests) < number:
            requests.append(base[len(requests) % len(base)])
    return requests
