# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read conversation rows from JSONL or Hugging Face sources and normalize them into tasks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from benchmarks.config.benchmark import BenchmarkConfig, ChatRequestDataset
from benchmarks.datasets.huggingface import (
    is_hf_dataset_spec,
    is_hf_file_uri,
    iter_hf_rows,
    resolve_hf_file_uri,
)


@dataclass(frozen=True)
class Turn:
    """One Chat Completions message, including fields beyond role and content."""

    role: str
    content: Any
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    """One benchmark episode input: the chat turns to run and source metadata such as tools."""

    id: str
    turns: tuple[Turn, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def messages(self) -> list[dict[str, Any]]:
        """Return the turns as OpenAI Chat Completions messages."""
        return [
            {"role": turn.role, "content": turn.content, **turn.extra}
            for turn in self.turns
        ]


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


def iter_dataset_rows(
    source: str | Path, *, allow_comments: bool = False
) -> Iterator[tuple[Path, int, int, Any]]:
    """Yield normalized rows from a local JSONL file, Hub file, or Hub dataset."""
    selector = str(source)
    local_path = Path(selector).expanduser()
    if local_path.is_file():
        yield from iter_jsonl_rows(local_path, allow_comments=allow_comments)
        return
    if is_hf_file_uri(selector):
        yield from iter_jsonl_rows(
            resolve_hf_file_uri(selector), allow_comments=allow_comments
        )
        return
    if is_hf_dataset_spec(selector):
        dataset_label = Path(f"hf://{selector}")
        for row_index, row in iter_hf_rows(selector):
            yield dataset_label, row_index + 1, row_index, row
        return
    raise FileNotFoundError(f"Dataset not found: {local_path}")


def _extract_row_content(
    row: Any,
    dataset_path: Path,
    line_number: int,
    *,
    allow_sharegpt: bool = False,
) -> tuple[Any, str | None, list[dict[str, Any]] | None]:
    """Extract the common OpenAI, prompt, user, and optional ShareGPT fields."""
    if isinstance(row, list):
        return row, None, None
    if not isinstance(row, dict):
        raise ValueError(
            f"Expected object or messages list at {dataset_path}:{line_number}"
        )

    tools = row.get("tools") or None
    if "messages" in row:
        return row["messages"], None, tools
    if "conversations" in row:
        if not allow_sharegpt:
            raise ValueError(
                "Line must be messages list or contain 'messages'/'prompt'/'user' "
                f"at {dataset_path}:{line_number}"
            )
        return _sharegpt_messages(row, dataset_path, line_number), None, tools
    if "prompt" in row:
        return None, str(row["prompt"]), tools
    if "user" in row:
        user_message = row["user"]
        if user_message is None or str(user_message) == "":
            raise ValueError(f"Empty user field at {dataset_path}:{line_number}")
        messages: list[dict[str, str]] = []
        system_message = row.get("system")
        if system_message is not None and str(system_message) != "":
            messages.append({"role": "system", "content": str(system_message)})
        messages.append({"role": "user", "content": str(user_message)})
        return messages, None, tools

    raise ValueError(
        "Line must be messages list or contain 'messages'/'prompt'/'user' "
        f"at {dataset_path}:{line_number}"
    )


def _message_turns(
    messages: Any,
    dataset_path: Path,
    line_number: int,
) -> tuple[Turn, ...]:
    """Convert a non-empty list of role/content messages into turns."""
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Invalid messages at {dataset_path}:{line_number}")
    turns: list[Turn] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or "role" not in message or ("content" not in message and not message.get("tool_calls")):
            raise ValueError(
                f"Invalid message {index} at {dataset_path}:{line_number}"
            )
        turns.append(
            Turn(
                role=str(message["role"]),
                content=message.get("content"),
                extra={
                    key: value
                    for key, value in message.items()
                    if key not in {"role", "content"}
                },
            )
        )
    return tuple(turns)


def _request_task(
    row: Any,
    dataset_path: Path,
    line_number: int,
    row_index: int,
) -> Task:
    """Read one row as an independent request whose tools, if any, travel in the task metadata."""
    messages, prompt, tools = _extract_row_content(row, dataset_path, line_number)
    metadata = {"tools": tools} if tools else {}
    if isinstance(row, dict):
        for key in ("tool_choice", "parallel_tool_calls"):
            if key in row:
                metadata[key] = row[key]
    if prompt is not None:
        turns: tuple[Turn, ...] = (Turn(role="user", content=prompt),)
    else:
        turns = _message_turns(messages, dataset_path, line_number)
    return Task(id=f"{dataset_path}:{row_index}", turns=turns, metadata=metadata)


def _sharegpt_messages(
    row: dict[str, Any],
    dataset_path: Path,
    line_number: int,
) -> list[dict[str, Any]]:
    """Convert a verified ShareGPT ``conversations/from/value`` row into messages."""
    conversations = row.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise ValueError(
            f"Invalid conversations at {dataset_path}:{line_number}"
        )
    role_map = {"human": "user", "gpt": "assistant"}
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(conversations):
        if not isinstance(message, dict):
            raise ValueError(
                f"Invalid ShareGPT message {index} at "
                f"{dataset_path}:{line_number}"
            )
        speaker = message.get("from")
        if speaker not in role_map or "value" not in message:
            raise ValueError(
                "ShareGPT messages must use from=human/gpt and value at "
                f"{dataset_path}:{line_number}"
            )
        messages.append(
            {"role": role_map[speaker], "content": message["value"]}
        )
    return messages


def _conversation_task(
    row: Any,
    dataset_path: Path,
    line_number: int,
    row_index: int,
) -> Task:
    """Read one OpenAI or ShareGPT record as a conversation the engine runs turn by turn."""
    messages, prompt, tools = _extract_row_content(
        row,
        dataset_path,
        line_number,
        allow_sharegpt=True,
    )
    if prompt is not None:
        messages = [{"role": "user", "content": prompt}]

    turns = _message_turns(messages, dataset_path, line_number)
    if not any(turn.role == "user" for turn in turns):
        raise ValueError(f"Conversation has no user message at {dataset_path}:{line_number}")
    fields = {"tools": tools} if tools else {}
    if isinstance(row, dict):
        for key in ("tool_choice", "parallel_tool_calls"):
            if key in row:
                fields[key] = row[key]
    task = Task(id=f"{dataset_path}:{row_index}", turns=turns, metadata=fields)
    split_chat_conversation(task.messages())
    return task


def split_chat_conversation(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split answer turns while retaining recorded tool calls and their results as context.

    A recorded call/result block is prefilled history, not a tool invocation.
    Ordinary reference answers are replaced by the benchmark engine's responses.
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    pending_tools: set[str] = set()
    for index, message in enumerate(messages):
        role = message["role"]
        calls = message.get("tool_calls") or []
        has_recorded_result = (
            index + 1 < len(messages) and messages[index + 1]["role"] == "tool"
        )
        if role == "assistant" and calls and has_recorded_result:
            if pending_tools:
                raise ValueError("Recorded tool calls require results before another call")
            ids = [call.get("id") for call in calls]
            if (
                any(not isinstance(value, str) or not value for value in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("Recorded tool calls require distinct non-empty IDs")
            pending_tools.update(ids)
            current.append(message)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending_tools:
                raise ValueError("Recorded tool result has no matching pending tool_call_id")
            pending_tools.remove(call_id)
            current.append(message)
        else:
            if pending_tools:
                raise ValueError("Recorded tool calls need matching results; tool execution requires a harness")
            if role == "assistant":
                if current:
                    turns.append(current)
                    current = []
            else:
                current.append(message)
    if pending_tools:
        raise ValueError("Recorded tool calls need matching results; tool execution requires a harness")
    if current:
        turns.append(current)
    return turns


def _load_dataset_tasks(
    dataset_selector: str,
    requested_count: int,
    row_offset: int,
    normalize: Callable[[Any, Path, int, int], Task],
    kind: str,
) -> list[Task]:
    """Consume one shared dataset-row iterator into requests or conversations."""
    tasks: list[Task] = []
    for dataset_path, line_number, row_index, row in iter_dataset_rows(
        dataset_selector
    ):
        if row_index < row_offset:
            continue
        tasks.append(normalize(row, dataset_path, line_number, row_index))
        if len(tasks) >= requested_count:
            break
    if len(tasks) < requested_count:
        raise ValueError(
            f"Loaded {len(tasks)} {kind} from {dataset_selector!r} "
            f"(offset={row_offset}), need {requested_count}"
        )
    return tasks


def load_conversation_tasks(benchmark: BenchmarkConfig) -> list[Task]:
    """Read complete conversation scripts for EvalScope interactive multi-turn runs."""
    workload = benchmark.resolved_workload
    conversation_count = benchmark.load.request_count
    row_offset = int(workload.row_offset)
    if workload.fixed_prompt and not workload.dataset_selectors:
        return [
            Task(
                id=f"prompt:{index}",
                turns=(Turn(role="user", content=workload.fixed_prompt),),
            )
            for index in range(conversation_count)
        ]

    if len(workload.dataset_selectors) != 1:
        raise ValueError(
            "A conversation child run requires exactly one dataset source"
        )
    dataset_selector = workload.dataset_selectors[0]
    if dataset_selector == "random":
        raise ValueError("EvalScope owns standard random dataset generation")
    return _load_dataset_tasks(
        dataset_selector,
        conversation_count,
        row_offset,
        _conversation_task,
        "conversations",
    )


def load_indexed_request_tasks(
    dataset_selector: str,
    row_indexes: list[int],
) -> list[Task]:
    """Read request tasks by dataset row index while preserving ``row_indexes`` order."""
    if not row_indexes:
        return []
    if dataset_selector == "random":
        raise ValueError("Indexed request loading does not support random data")

    requested_indexes = set(row_indexes)
    loaded: dict[int, Task] = {}
    for dataset_label, line_number, row_index, row in iter_dataset_rows(
        dataset_selector
    ):
        if row_index in requested_indexes:
            loaded[row_index] = _request_task(
                row, dataset_label, line_number, row_index
            )
            if len(loaded) == len(requested_indexes):
                break

    missing_indexes = sorted(requested_indexes - loaded.keys())
    if missing_indexes:
        raise ValueError(
            f"Dataset {dataset_selector!r} is missing row indexes "
            f"{missing_indexes[:10]}"
        )
    return [loaded[row_index] for row_index in row_indexes]


def load_request_tasks(
    benchmark: BenchmarkConfig,
    *,
    dataset_selector: Optional[str] = None,
    request_count: Optional[int] = None,
) -> list[Task]:
    """Read the independent Chat Completions requests required by one HTTP workload."""
    workload: ChatRequestDataset = benchmark.resolved_workload
    count = (
        benchmark.load.request_count
        if request_count is None
        else request_count
    )
    row_offset = int(workload.row_offset)

    if workload.fixed_prompt and dataset_selector is None:
        return [
            Task(
                id=f"prompt:{index}",
                turns=(Turn(role="user", content=workload.fixed_prompt),),
            )
            for index in range(count)
        ]

    if dataset_selector is None:
        if not workload.dataset_selectors:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL | org/name[:split] | "
                "hf://datasets/...)."
            )
        if len(workload.dataset_selectors) != 1:
            raise ValueError(
                "load_request_tasks requires dataset_selector= when multiple "
                "--dataset values are configured"
            )
        dataset_selector = workload.dataset_selectors[0]

    if dataset_selector == "random":
        raise ValueError("EvalScope owns standard random dataset generation")

    return _load_dataset_tasks(
        dataset_selector, count, row_offset, _request_task, "requests"
    )
