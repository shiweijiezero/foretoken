"""Load benchmark requests via EvalScope perf dataset plugins.

Mirrors ``evalscope perf`` dataset flags:

- ``--dataset``: EvalScope plugin name, **or** multi-dataset schedule
  (``sequential`` / ``mixed``)
- ``--dataset-path``: one or more file/dir paths (list; multi-path with
  ``sequential``/``mixed`` = Phase 3)
- ``--dataset-offset`` / ``--tokenizer-path`` / prompt-length flags
- ``--prefix-length`` / ``--apply-chat-template`` / ``--prompt``
- ``--max-turns`` (for ``custom_multi_turn`` / share_gpt multi-turn)

Reuses ``evalscope.perf.plugin.DatasetRegistry``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from benchmarks.arguments import BenchArguments
from benchmarks.deps.evalscope import require_datasets
from benchmarks.workload.jsonl_loader import iter_jsonl

# Repo workload files → EvalScope perf dataset plugin names.
KNOWN_DATASET_FILES: dict[str, str] = {
    "conversation.jsonl": "custom_multi_turn",
    "toolagent.jsonl": "custom_multi_turn",
    "mooncake.jsonl": "custom_multi_turn",
    "merged.jsonl": "custom_multi_turn",
}


def _resolve_dataset(args: BenchArguments) -> tuple[str, Optional[str]]:
    """Resolve (plugin name, dataset_path) like evalscope perf.

    Preferred: ``--dataset`` = EvalScope plugin **or** (for Phase 3)
    ``sequential``/``mixed`` with multiple ``--dataset-path``.
    Compatibility: a bare ``.jsonl`` / ``.txt`` / ``.json`` in ``--dataset``
    is treated as a path and the plugin is inferred (known repo files →
    ``custom_multi_turn``, else ``line_by_line``).
    """
    path = (args.primary_dataset_path or "").strip() or None
    name = (args.dataset or "").strip() or "openqa"

    # Compatibility: bare path mistakenly passed as --dataset
    if name.endswith((".jsonl", ".txt", ".json")):
        path = path or name
        basename = Path(path).name
        plugin = KNOWN_DATASET_FILES.get(basename, "line_by_line")
        return plugin, path

    # Explicit path + known basename (even if --dataset still default openqa)
    if path:
        basename = Path(path).name
        if basename in KNOWN_DATASET_FILES and name in (
            "openqa",
            "line_by_line",
            "jsonl",
            "",
        ):
            return KNOWN_DATASET_FILES[basename], path

    return name, path


def _to_evalscope_args(
    args: BenchArguments,
    dataset: str,
    dataset_path: Optional[str],
):
    Arguments, _, _ = require_datasets()
    number = max(args.number) if args.number else args.primary_number
    return Arguments(
        model=args.model,
        url=args.url,
        api_key=args.api_key or None,
        number=number,
        parallel=args.primary_parallel,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        stream=args.stream,
        dataset=dataset,
        dataset_path=dataset_path,
        dataset_offset=args.dataset_offset,
        tokenizer_path=args.tokenizer_path or None,
        min_prompt_length=args.min_prompt_length,
        max_prompt_length=args.max_prompt_length,
        prefix_length=args.prefix_length,
        apply_chat_template=args.apply_chat_template,
        prompt=args.prompt or None,
        max_turns=args.max_turns,
    )


def _truncate_conversation(
    messages: list[dict[str, Any]],
    max_turns: Optional[int],
) -> list[dict[str, Any]]:
    """Keep messages through the N-th user turn (evalscope max_turns semantics)."""
    if max_turns is None or max_turns <= 0:
        return messages
    out: list[dict[str, Any]] = []
    user_turns = 0
    for msg in messages:
        out.append(msg)
        if msg.get("role") == "user":
            user_turns += 1
            if user_turns >= max_turns:
                break
    return out


def _load_custom_multi_turn(
    dataset_path: str,
    *,
    limit: int,
    max_turns: Optional[int],
) -> list[dict[str, Any]]:
    """Load OpenAI message-array JSONL as single chat requests.

    Each line must be a JSON array of ``{role, content}`` (same as EvalScope
    ``custom_multi_turn``). Until foretoken has a multi-turn runner, each
    conversation is sent as **one** request with the full message history
    (including dataset assistant turns), optionally truncated by ``max_turns``.
    """
    path = Path(dataset_path)
    requests: list[dict[str, Any]] = []
    for line_no, messages in iter_jsonl(path):
        if not isinstance(messages, list) or not messages:
            raise ValueError(
                f"custom_multi_turn expects a non-empty message array "
                f"at {path}:{line_no}"
            )
        if not all(
            isinstance(m, dict) and "role" in m and "content" in m
            for m in messages
        ):
            raise ValueError(
                f"Each message needs role/content at {path}:{line_no}"
            )
        messages = _truncate_conversation(messages, max_turns)
        if not messages:
            continue
        requests.append({"messages": messages})
        if len(requests) >= limit:
            break

    if not requests:
        raise ValueError(
            f"No conversations loaded from custom_multi_turn path={path}"
        )
    return requests


def _normalize_item(item: Any, Turn: type) -> Optional[dict[str, Any]]:
    """Convert an EvalScope plugin yield into a foretoken request dict."""
    # Multi-turn Conversation: List[Turn] — first turn only until multi-turn runner.
    if isinstance(item, list) and item and isinstance(item[0], Turn):
        first = item[0].messages
        if not first:
            return None
        return {"messages": list(first)}

    if isinstance(item, dict):
        if "messages" in item or "prompt" in item:
            request = {
                k: item[k]
                for k in ("messages", "prompt", "tools")
                if k in item and item[k]
            }
            return request or None
        return item

    if isinstance(item, str):
        return {"prompt": item}

    if isinstance(item, list):
        if not item:
            return None
        if isinstance(item[0], int):
            raise NotImplementedError(
                "Token-ID prompts (evalscope --tokenize-prompt) "
                "are not supported yet"
            )
        if isinstance(item[0], dict) and "role" in item[0]:
            return {"messages": item}
        raise TypeError(f"Unsupported dataset item list: {type(item[0])}")

    raise TypeError(f"Unsupported dataset item type: {type(item)}")


class EvalscopeDatasetLoader:
    """Build request payloads using EvalScope perf registered dataset plugins."""

    def __init__(
        self,
        args: BenchArguments,
        limit: Optional[int] = None,
    ):
        self.args = args
        self.limit = limit

    def load(self) -> list[dict[str, Any]]:
        _, DatasetRegistry, Turn = require_datasets()
        dataset, dataset_path = _resolve_dataset(self.args)
        limit = (
            self.limit
            if self.limit is not None
            else (
                max(self.args.number)
                if self.args.number
                else self.args.primary_number
            )
        )

        # conversation.jsonl-style: full history as one chat request.
        if dataset == "custom_multi_turn":
            if not dataset_path:
                raise ValueError(
                    "custom_multi_turn requires --dataset-path "
                    "(e.g. conversation.jsonl)"
                )
            return _load_custom_multi_turn(
                dataset_path,
                limit=limit,
                max_turns=self.args.max_turns,
            )

        if dataset == "random" and not (self.args.tokenizer_path or "").strip():
            raise ValueError(
                "--dataset random requires --tokenizer-path "
                "(same as evalscope perf)"
            )

        es_args = _to_evalscope_args(self.args, dataset, dataset_path)
        try:
            plugin_cls = DatasetRegistry.get_class(dataset)
        except ValueError as e:
            known = ", ".join(sorted(DatasetRegistry.all_classes()))
            raise ValueError(
                f"Unknown dataset '{dataset}'. "
                f"EvalScope perf plugins: {known}"
            ) from e

        plugin = plugin_cls(es_args)

        requests: list[dict[str, Any]] = []
        for item in plugin.build_messages():
            request = _normalize_item(item, Turn)
            if request is None:
                continue
            requests.append(request)
            if len(requests) >= limit:
                break

        if not requests:
            raise ValueError(
                f"No requests loaded from dataset={dataset!r} "
                f"path={dataset_path!r}"
            )
        return requests
