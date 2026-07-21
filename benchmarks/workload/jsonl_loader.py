from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple


def iter_jsonl(path: Path | str) -> Iterator[Tuple[int, Any]]:
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
                raise ValueError(
                    f"Invalid JSON at {p}:{line_no}: {e}"
                ) from e


class JsonlPromptLoader:
    """Load OpenAI-style chat requests from a JSONL file.

    Supported line formats:
    - list of messages: [{"role": "...", "content": "..."}, ...]
    - object with messages: {"messages": [...], "tools": [...]?}
    - object with prompt: {"prompt": "..."}
    """

    def __init__(
        self,
        path: str,
        limit: Optional[int] = None,
    ):
        self.path = Path(path)
        self.limit = limit

    def load(self) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for line_no, obj in iter_jsonl(self.path):
            requests.append(self._normalize(obj, line_no))
            if self.limit is not None and len(requests) >= self.limit:
                break

        if not requests:
            raise ValueError(f"No requests loaded from {self.path}")
        return requests

    def _normalize(
        self,
        obj: Any,
        line_no: int,
    ) -> dict[str, Any]:
        if isinstance(obj, list):
            if not obj:
                raise ValueError(
                    f"Empty messages list at {self.path}:{line_no}"
                )
            return {"messages": obj}

        if not isinstance(obj, dict):
            raise ValueError(
                f"Expected object or messages list at {self.path}:{line_no}"
            )

        if "messages" in obj:
            messages = obj["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError(
                    f"Invalid messages at {self.path}:{line_no}"
                )
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
            f"Line must be messages list or contain "
            f"'messages'/'prompt' at {self.path}:{line_no}"
        )
