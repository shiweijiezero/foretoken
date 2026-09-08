# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build requests and conversations from a fixed prompt, JSONL, or Hugging Face data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from benchmarks.performance.benchmark_config import (
    ChatRequestDataset,
    HttpBenchmarkConfig,
)
from benchmarks.performance.chat_client import ChatRequestContent
from benchmarks.performance.huggingface_datasets import (
    is_hf_file_uri,
    iter_hf_rows,
    resolve_hf_file_uri,
)
from benchmarks.performance.jsonl import load_jsonl


def _normalize_chat_request(
    row: Any,
    dataset_path: Path,
    line_number: int,
) -> ChatRequestContent:
    if isinstance(row, list):
        if not row:
            raise ValueError(
                f"Empty messages list at {dataset_path}:{line_number}"
            )
        return ChatRequestContent(messages=row)

    if not isinstance(row, dict):
        raise ValueError(
            f"Expected object or messages list at {dataset_path}:{line_number}"
        )

    tools = row.get("tools") or None
    if "messages" in row:
        messages = row["messages"]
        if not isinstance(messages, list) or not messages:
            raise ValueError(
                f"Invalid messages at {dataset_path}:{line_number}"
            )
        return ChatRequestContent(messages=messages, tools=tools)

    if "prompt" in row:
        return ChatRequestContent(prompt=str(row["prompt"]), tools=tools)

    if "user" in row:
        user_message = row["user"]
        if user_message is None or str(user_message) == "":
            raise ValueError(
                f"Empty user field at {dataset_path}:{line_number}"
            )
        messages: list[dict[str, str]] = []
        system_message = row.get("system")
        if system_message is not None and str(system_message) != "":
            messages.append(
                {"role": "system", "content": str(system_message)}
            )
        messages.append({"role": "user", "content": str(user_message)})
        return ChatRequestContent(messages=messages, tools=tools)

    raise ValueError(
        "Line must be messages list or contain 'messages'/'prompt'/'user' "
        f"at {dataset_path}:{line_number}"
    )


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


def _normalize_chat_conversation(
    row: Any,
    dataset_path: Path,
    line_number: int,
) -> list[dict[str, Any]]:
    """Read one OpenAI or ShareGPT record for EvalScope to run as a real conversation."""
    if isinstance(row, list):
        messages = row
        tools = None
    elif isinstance(row, dict):
        tools = row.get("tools")
        if "messages" in row:
            messages = row["messages"]
        elif "conversations" in row:
            messages = _sharegpt_messages(row, dataset_path, line_number)
        elif "prompt" in row:
            messages = [{"role": "user", "content": str(row["prompt"])}]
        elif "user" in row:
            if row["user"] is None or str(row["user"]) == "":
                raise ValueError(
                    f"Empty user field at {dataset_path}:{line_number}"
                )
            messages = []
            system_message = row.get("system")
            if system_message is not None and str(system_message) != "":
                messages.append(
                    {"role": "system", "content": str(system_message)}
                )
            messages.append({"role": "user", "content": str(row["user"])})
        else:
            raise ValueError(
                "Conversation rows must contain 'messages', verified ShareGPT "
                "'conversations', 'prompt', or 'user' at "
                f"{dataset_path}:{line_number}"
            )
    else:
        raise ValueError(
            f"Expected conversation object or list at "
            f"{dataset_path}:{line_number}"
        )

    if tools:
        raise ValueError(
            "Conversation mode does not support top-level tools because EvalScope's "
            "ordinary conversation runner does not execute tool calls"
        )
    if not isinstance(messages, list) or not messages:
        raise ValueError(
            f"Invalid messages at {dataset_path}:{line_number}"
        )
    if not any(
        isinstance(message, dict) and message.get("role") == "user"
        for message in messages
    ):
        raise ValueError(
            f"Conversation has no user message at {dataset_path}:{line_number}"
        )
    turn_has_messages = False
    turn_has_user = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not {"role", "content"} <= message.keys():
            raise ValueError(
                f"Invalid conversation message {index} at "
                f"{dataset_path}:{line_number}"
            )
        role = message["role"]
        if role == "assistant":
            if turn_has_messages and not turn_has_user:
                raise ValueError(
                    "Each multi-turn delta must contain a user message at "
                    f"{dataset_path}:{line_number}"
                )
            turn_has_messages = False
            turn_has_user = False
        else:
            turn_has_messages = True
            turn_has_user = turn_has_user or role == "user"
        if role != "assistant" and message["content"] is None:
            raise ValueError(
                f"Empty conversation message {index} at "
                f"{dataset_path}:{line_number}"
            )
        if (
            role == "tool"
            or "tool_calls" in message
            or "tool_call_id" in message
        ):
            raise ValueError(
                "Conversation mode does not support scripted tool messages or tool "
                "calls; use user/assistant conversation data"
            )
    if turn_has_messages and not turn_has_user:
        raise ValueError(
            "Each multi-turn delta must contain a user message at "
            f"{dataset_path}:{line_number}"
        )
    return messages


def split_chat_conversation(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split at reference assistant boundaries into EvalScope user-turn increments."""
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "assistant":
            if current:
                turns.append(current)
                current = []
        else:
            current.append(message)
    if current:
        turns.append(current)
    return turns


def _load_jsonl_requests(
    dataset_path: str,
    request_count: int,
    row_offset: int,
) -> list[ChatRequestContent]:
    path = Path(dataset_path)
    requests: list[ChatRequestContent] = []
    for line_number, row in load_jsonl(path):
        if line_number <= row_offset:
            continue
        requests.append(_normalize_chat_request(row, path, line_number))
        if len(requests) >= request_count:
            break

    if len(requests) < request_count:
        raise ValueError(
            f"Loaded {len(requests)} requests from {path} "
            f"(offset={row_offset}), need {request_count}"
        )
    return requests


def _load_huggingface_requests(
    dataset_selector: str,
    request_count: int,
    row_offset: int,
) -> list[ChatRequestContent]:
    dataset_label = Path(f"hf://{dataset_selector}")
    requests: list[ChatRequestContent] = []
    for row_index, row in iter_hf_rows(dataset_selector):
        if row_index < row_offset:
            continue
        requests.append(
            _normalize_chat_request(row, dataset_label, row_index + 1)
        )
        if len(requests) >= request_count:
            break

    if len(requests) < request_count:
        raise ValueError(
            f"Loaded {len(requests)} requests from Hugging Face "
            f"{dataset_selector!r} (offset={row_offset}), need {request_count}"
        )
    return requests


def _load_jsonl_conversations(
    dataset_path: str,
    conversation_count: int,
    row_offset: int,
) -> list[list[dict[str, Any]]]:
    path = Path(dataset_path)
    conversations: list[list[dict[str, Any]]] = []
    for line_number, row in load_jsonl(path):
        if line_number <= row_offset:
            continue
        conversations.append(
            _normalize_chat_conversation(row, path, line_number)
        )
        if len(conversations) >= conversation_count:
            break
    if len(conversations) < conversation_count:
        raise ValueError(
            f"Loaded {len(conversations)} conversations from {path} "
            f"(offset={row_offset}), need {conversation_count}"
        )
    return conversations


def _load_huggingface_conversations(
    dataset_selector: str,
    conversation_count: int,
    row_offset: int,
) -> list[list[dict[str, Any]]]:
    dataset_label = Path(f"hf://{dataset_selector}")
    conversations: list[list[dict[str, Any]]] = []
    for row_index, row in iter_hf_rows(dataset_selector):
        if row_index < row_offset:
            continue
        conversations.append(
            _normalize_chat_conversation(
                row, dataset_label, row_index + 1
            )
        )
        if len(conversations) >= conversation_count:
            break
    if len(conversations) < conversation_count:
        raise ValueError(
            f"Loaded {len(conversations)} conversations from Hugging Face "
            f"{dataset_selector!r} (offset={row_offset}), "
            f"need {conversation_count}"
        )
    return conversations


def load_chat_conversations(
    benchmark: HttpBenchmarkConfig,
) -> list[list[dict[str, Any]]]:
    """Read complete conversation scripts for EvalScope interactive multi-turn runs."""
    dataset = benchmark.request_dataset
    conversation_count = benchmark.load_schedule.request_count
    row_offset = int(dataset.row_offset)
    if dataset.fixed_prompt and not dataset.dataset_selectors:
        return [
            [{"role": "user", "content": dataset.fixed_prompt}]
            for _ in range(conversation_count)
        ]

    if len(dataset.dataset_selectors) != 1:
        raise ValueError(
            "A conversation child run requires exactly one dataset source"
        )
    dataset_selector = dataset.dataset_selectors[0]
    if dataset_selector == "random":
        from benchmarks.performance.synthetic_requests import generate_random_requests

        requests = generate_random_requests(
            benchmark, request_count=conversation_count
        )
        return [
            request.messages
            if request.messages is not None
            else [{"role": "user", "content": request.prompt}]
            for request in requests
        ]
    if is_hf_file_uri(dataset_selector) or dataset_selector.startswith("hf://"):
        local_path = resolve_hf_file_uri(dataset_selector)
        return _load_jsonl_conversations(
            local_path, conversation_count, row_offset
        )
    local_path = Path(dataset_selector)
    if local_path.is_file():
        return _load_jsonl_conversations(
            str(local_path), conversation_count, row_offset
        )
    return _load_huggingface_conversations(
        dataset_selector, conversation_count, row_offset
    )


def load_indexed_chat_requests(
    dataset_selector: str,
    row_indexes: list[int],
) -> list[ChatRequestContent]:
    """Read chat requests by dataset row index while preserving ``row_indexes`` order."""
    if not row_indexes:
        return []
    if dataset_selector == "random":
        raise ValueError("Indexed request loading does not support random data")

    requested_indexes = set(row_indexes)
    loaded: dict[int, ChatRequestContent] = {}
    local_path = Path(dataset_selector)
    if local_path.is_file():
        dataset_label = local_path
        rows = (
            (row_index, row, line_number)
            for row_index, (line_number, row) in enumerate(load_jsonl(local_path))
        )
    else:
        dataset_label = Path(f"hf://{dataset_selector}")
        rows = (
            (row_index, row, row_index + 1)
            for row_index, row in iter_hf_rows(dataset_selector)
        )

    for row_index, row, line_number in rows:
        if row_index in requested_indexes:
            loaded[row_index] = _normalize_chat_request(
                row, dataset_label, line_number
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


def load_chat_requests(
    benchmark: HttpBenchmarkConfig,
    *,
    dataset_selector: Optional[str] = None,
    request_count: Optional[int] = None,
) -> list[ChatRequestContent]:
    """Read the independent Chat Completions requests required by one HTTP workload."""
    dataset: ChatRequestDataset = benchmark.request_dataset
    count = (
        benchmark.load_schedule.request_count
        if request_count is None
        else request_count
    )
    row_offset = int(dataset.row_offset)

    if dataset.fixed_prompt and dataset_selector is None:
        return [
            ChatRequestContent(prompt=dataset.fixed_prompt) for _ in range(count)
        ]

    if dataset_selector is None:
        if not dataset.dataset_selectors:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL | org/name:split | "
                "hf://datasets/...)."
            )
        if len(dataset.dataset_selectors) != 1:
            raise ValueError(
                "load_chat_requests requires dataset_selector= when multiple "
                "--dataset values are configured"
            )
        dataset_selector = dataset.dataset_selectors[0]

    if dataset_selector == "random":
        from benchmarks.performance.synthetic_requests import generate_random_requests

        return generate_random_requests(benchmark, request_count=count)

    if is_hf_file_uri(dataset_selector) or dataset_selector.startswith("hf://"):
        local_path = resolve_hf_file_uri(dataset_selector)
        return _load_jsonl_requests(local_path, count, row_offset)

    local_path = Path(dataset_selector)
    if local_path.is_file():
        return _load_jsonl_requests(str(local_path), count, row_offset)

    return _load_huggingface_requests(dataset_selector, count, row_offset)
