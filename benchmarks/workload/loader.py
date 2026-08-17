# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Build request list from prompt, random, JSONL path, or HuggingFace dataset id."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from benchmarks.config import BenchConfig, DatasetConfig
from benchmarks.workload.hf_dataset import iter_hf_rows


def load_jsonl(path: Path | str) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_no, parsed_object)`` for non-empty JSONL lines."""
    jsonl_path = Path(path)
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"JSONL not found: {jsonl_path}")
    with jsonl_path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON at {jsonl_path}:{line_no}: {error}"
                ) from error


def _normalize(payload: Any, path: Path, line_no: int) -> dict[str, Any]:
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"Empty messages list at {path}:{line_no}")
        return {"messages": payload}

    if not isinstance(payload, dict):
        raise ValueError(f"Expected object or messages list at {path}:{line_no}")

    if "messages" in payload:
        messages = payload["messages"]
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Invalid messages at {path}:{line_no}")
        request: dict[str, Any] = {"messages": messages}
        if payload.get("tools"):
            request["tools"] = payload["tools"]
        return request

    if "prompt" in payload:
        request = {"prompt": str(payload["prompt"])}
        if payload.get("tools"):
            request["tools"] = payload["tools"]
        return request

    if "user" in payload:
        user = payload["user"]
        if user is None or str(user) == "":
            raise ValueError(f"Empty user field at {path}:{line_no}")
        messages: list[dict[str, str]] = []
        system = payload.get("system")
        if system is not None and str(system) != "":
            messages.append({"role": "system", "content": str(system)})
        messages.append({"role": "user", "content": str(user)})
        request = {"messages": messages}
        if payload.get("tools"):
            request["tools"] = payload["tools"]
        return request

    raise ValueError(
        f"Line must be messages list or contain 'messages'/'prompt'/'user' "
        f"at {path}:{line_no}"
    )


def _load_jsonl_requests(path: str, number: int, offset: int) -> list[dict[str, Any]]:
    jsonl_path = Path(path)
    requests: list[dict[str, Any]] = []
    for line_no, payload in load_jsonl(jsonl_path):
        if line_no <= offset:
            continue
        requests.append(_normalize(payload, jsonl_path, line_no))
        if len(requests) >= number:
            break

    if len(requests) < number:
        raise ValueError(
            f"Loaded {len(requests)} requests from {jsonl_path} "
            f"(offset={offset}), need {number}"
        )
    return requests


def _load_hf_requests(spec: str, number: int, offset: int) -> list[dict[str, Any]]:
    label = Path(f"hf://{spec}")
    requests: list[dict[str, Any]] = []
    for row_index, payload in iter_hf_rows(spec):
        if row_index < offset:
            continue
        requests.append(_normalize(payload, label, row_index + 1))
        if len(requests) >= number:
            break

    if len(requests) < number:
        raise ValueError(
            f"Loaded {len(requests)} requests from HuggingFace {spec!r} "
            f"(offset={offset}), need {number}"
        )
    return requests


def load_requests(
    config: BenchConfig,
    *,
    source: Optional[str] = None,
    number: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Load requests from prompt, random, local JSONL, or HuggingFace dataset id.

    ``source`` / ``number`` override the config when a multi-dataset runner
    loads one share of the total request count.
    """
    dataset: DatasetConfig = config.dataset
    count = config.load.number[0] if number is None else number

    if dataset.prompt and source is None:
        return [{"prompt": dataset.prompt} for _ in range(count)]

    if source is None:
        if not dataset.dataset:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL path | HuggingFace id)."
            )
        if len(dataset.dataset) != 1:
            raise ValueError(
                "load_requests requires source= when multiple --dataset values"
            )
        source = dataset.dataset[0]

    if source == "random":
        if number is not None and number != config.load.number[0]:
            raise ValueError(
                "number override is not supported for --dataset random"
            )
        from benchmarks.workload.random_dataset import generate_random_requests

        return generate_random_requests(config)

    path = Path(source)
    if path.is_file():
        return _load_jsonl_requests(
            str(path), number=count, offset=int(dataset.dataset_offset)
        )

    return _load_hf_requests(
        source, number=count, offset=int(dataset.dataset_offset)
    )
