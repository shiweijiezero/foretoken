# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read dataset rows and normalize them into requests or conversations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from benchmarks.performance.config import ChatRequestDataset, HttpBenchmarkConfig
from benchmarks.performance.chat_client import ChatRequestContent


_HF_DATASETS_PREFIX = "hf://datasets/"
_HF_FILE_URI_FORMAT = "hf://datasets/<org>/<repo>[@<revision>]/<path>"
_DEFAULT_SELECTORS = {
    "KrisQ/StudyChat": "train",
    "valeriol29/mooncake-traces": "conversation",
}


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


def is_hf_file_uri(source: str) -> bool:
    """Return whether a source is a canonical Hugging Face dataset file URI."""
    return source.startswith(_HF_DATASETS_PREFIX)


def parse_hf_file_uri(uri: str) -> tuple[str, Optional[str], str]:
    """Parse a Hugging Face dataset file URI into repository, revision, and path."""
    if not is_hf_file_uri(uri):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
        )
    remainder = uri[len(_HF_DATASETS_PREFIX) :]
    if not remainder or remainder.startswith("/"):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
        )
    if "@" in remainder:
        repo_id, after_at = remainder.split("@", 1)
        if not repo_id or "/" not in after_at:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
            )
        revision, filename = after_at.split("/", 1)
        if not revision or not filename:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
            )
        return repo_id, revision, filename
    parts = remainder.split("/")
    if len(parts) >= 3 and all(parts):
        return f"{parts[0]}/{parts[1]}", None, "/".join(parts[2:])
    if len(parts) == 2 and all(parts):
        return parts[0], None, parts[1]
    raise ValueError(
        f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
    )


def resolve_hf_file_uri(uri: str) -> str:
    """Download a Hugging Face dataset file and return its local cache path."""
    from huggingface_hub import hf_hub_download

    repo_id, revision, filename = parse_hf_file_uri(uri)
    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )


def parse_hf_dataset_spec(spec: str) -> tuple[str, str]:
    """Parse a Hugging Face dataset selector into dataset ID and split/config."""
    if ":" not in spec:
        selector = _DEFAULT_SELECTORS.get(spec)
        if selector is not None:
            return spec, selector
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. "
            "Use 'org/name:split' (split is required)."
        )
    dataset_id, split = spec.rsplit(":", 1)
    if not dataset_id or not split:
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. Use 'org/name:split'."
        )
    return dataset_id, split


def is_hf_dataset_spec(spec: str) -> bool:
    """Return whether a selector names a supported Hugging Face dataset."""
    try:
        parse_hf_dataset_spec(spec)
    except ValueError:
        return False
    return True


def same_dataset_selector(left: str, right: str) -> bool:
    """Return whether two selectors resolve to the same Hugging Face dataset."""
    if left == right:
        return True
    try:
        return parse_hf_dataset_spec(left) == parse_hf_dataset_spec(right)
    except ValueError:
        return False


def _load_hf_data(dataset_id: str, split: str) -> Any:
    """Stream one Hugging Face split or builder configuration."""
    from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset

    configs = get_dataset_config_names(dataset_id)
    if split in configs:
        data_splits = get_dataset_split_names(dataset_id, split)
        if len(data_splits) != 1:
            raise ValueError(
                f"Hugging Face dataset {dataset_id!r} config {split!r} has "
                f"multiple data splits {data_splits}; expected exactly one."
            )
        return load_dataset(
            dataset_id, name=split, split=data_splits[0], streaming=True
        )
    return load_dataset(dataset_id, split=split, streaming=True)


def iter_hf_rows(spec: str) -> Iterator[tuple[int, Any]]:
    """Yield zero-based row indexes and values from a Hugging Face selector."""
    dataset_id, split = parse_hf_dataset_spec(spec)
    for row_index, row in enumerate(_load_hf_data(dataset_id, split)):
        yield row_index, dict(row)


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


def _normalize_chat_request(
    row: Any,
    dataset_path: Path,
    line_number: int,
) -> ChatRequestContent:
    messages, prompt, tools = _extract_row_content(row, dataset_path, line_number)
    if prompt is not None:
        return ChatRequestContent(prompt=prompt, tools=tools)
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Invalid messages at {dataset_path}:{line_number}")
    return ChatRequestContent(messages=messages, tools=tools)


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
    messages, prompt, tools = _extract_row_content(
        row,
        dataset_path,
        line_number,
        allow_sharegpt=True,
    )
    if prompt is not None:
        messages = [{"role": "user", "content": prompt}]

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


def _load_dataset_values(
    dataset_selector: str,
    requested_count: int,
    row_offset: int,
    consumer: Callable[[Any, Path, int], Any],
    kind: str,
) -> list[Any]:
    """Consume one shared dataset-row iterator into requests or conversations."""
    values: list[Any] = []
    for dataset_path, line_number, row_index, row in iter_dataset_rows(
        dataset_selector
    ):
        if row_index < row_offset:
            continue
        values.append(consumer(row, dataset_path, line_number))
        if len(values) >= requested_count:
            break
    if len(values) < requested_count:
        raise ValueError(
            f"Loaded {len(values)} {kind} from {dataset_selector!r} "
            f"(offset={row_offset}), need {requested_count}"
        )
    return values


def _load_dataset_requests(
    dataset_selector: str,
    request_count: int,
    row_offset: int,
) -> list[ChatRequestContent]:
    return _load_dataset_values(
        dataset_selector,
        request_count,
        row_offset,
        _normalize_chat_request,
        "requests",
    )


def _load_dataset_conversations(
    dataset_selector: str,
    conversation_count: int,
    row_offset: int,
) -> list[list[dict[str, Any]]]:
    return _load_dataset_values(
        dataset_selector,
        conversation_count,
        row_offset,
        _normalize_chat_conversation,
        "conversations",
    )


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
    return _load_dataset_conversations(
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
    for dataset_label, line_number, row_index, row in iter_dataset_rows(
        dataset_selector
    ):
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

    return _load_dataset_requests(dataset_selector, count, row_offset)
