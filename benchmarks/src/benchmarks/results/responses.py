# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Write optional request and assembled response evidence as requests finish."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


class ResponseWriter:
    """Own separate JSONL files for warmup and measured response content."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.files: dict[str, TextIO] = {}

    def write(self, phase: str, record: dict[str, Any]) -> None:
        """Persist one completed or interrupted request without retaining other responses."""
        if phase not in self.files:
            name = "warmup_responses.jsonl" if phase == "warmup" else "responses.jsonl"
            self.files[phase] = (self.directory / name).open("w", encoding="utf-8")
        file = self.files[phase]
        file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        file.flush()

    def close(self) -> None:
        """Close every opened response file at the owning result lifecycle's exit."""
        for file in self.files.values():
            file.close()
        self.files.clear()


def merge_delta(target: dict[str, Any], delta: dict[str, Any]) -> None:
    """Assemble OpenAI message fragments, including indexed tool-call arguments."""
    for key, value in delta.items():
        if value is None:
            target.setdefault(key, None)
        elif isinstance(value, str) and key not in {"role", "type", "id"}:
            target[key] = (target.get(key) or "") + value
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            merge_delta(target[key], value)
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) and "index" in item for item in value)
        ):
            current = target.setdefault(key, [])
            for fragment in value:
                index = fragment["index"]
                item = next(
                    (item for item in current if item.get("index") == index), None
                )
                if item is None:
                    item = {"index": index}
                    current.append(item)
                merge_delta(item, fragment)
        elif isinstance(value, list):
            target.setdefault(key, []).extend(value)
        else:
            target[key] = value


class ResponseContent:
    """Count returned text while retaining content only for raw output or generated history."""

    def __init__(self, *, raw: bool, history: bool) -> None:
        self.raw = raw
        self.history = history
        self.response: dict[str, Any] = {}
        self.choices: dict[int, dict[str, Any]] = {}
        self.content_characters: int | None = None
        self.reasoning_characters: int | None = None
        self.finish_reason: str | None = None
        self.response_model: str | None = None
        self.response_id: str | None = None
        self.usage: dict[str, Any] | None = None
        self.tool_calls = False
        self.generated: list[str] = []

    def observe(
        self, payload: dict[str, Any], *, streaming: bool, completion: bool
    ) -> None:
        """Consume a response/chunk once, preserving absence and explicit empty text."""
        self.response_id = payload.get("id", self.response_id)
        self.response_model = payload.get("model", self.response_model)
        if payload.get("usage") is not None:
            self.usage = payload["usage"]
        if self.raw:
            self.response.update(
                {key: value for key, value in payload.items() if key != "choices"}
            )
        for choice in payload.get("choices", []):
            index = choice.get("index", 0)
            message = (
                {"content": choice.get("text")}
                if completion
                else choice.get("delta" if streaming else "message", {})
            )
            if index == 0:
                if choice.get("finish_reason") is not None:
                    self.finish_reason = choice["finish_reason"]
                content = message.get("content")
                if isinstance(content, str):
                    self.content_characters = (self.content_characters or 0) + len(
                        content
                    )
                    if self.history:
                        self.generated.append(content)
                reasoning = message.get("reasoning_content", message.get("reasoning"))
                if isinstance(reasoning, str):
                    self.reasoning_characters = (self.reasoning_characters or 0) + len(
                        reasoning
                    )
                self.tool_calls = self.tool_calls or bool(
                    message.get("tool_calls") or message.get("function_call")
                )
            if self.raw:
                assembled = self.choices.setdefault(index, {"index": index})
                assembled.update(
                    {
                        key: value
                        for key, value in choice.items()
                        if key not in {"delta", "message", "text", "logprobs"}
                        and value is not None
                    }
                )
                if streaming:
                    if completion:
                        assembled["text"] = assembled.get("text", "") + (
                            choice.get("text") or ""
                        )
                    else:
                        merge_delta(assembled.setdefault("message", {}), message)
                    if choice.get("logprobs") is not None:
                        merge_delta(
                            assembled.setdefault("logprobs", {}), choice["logprobs"]
                        )
                else:
                    assembled.update(choice)

    def record(self) -> dict[str, Any]:
        """Return the assembled response without any original SSE framing or timing arrays."""
        return {
            **self.response,
            "choices": [self.choices[index] for index in sorted(self.choices)],
        }
