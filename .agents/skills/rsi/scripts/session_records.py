# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read visible Codex or Claude Code work records with original JSONL locations."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def timestamp(value: str) -> datetime:
    """Parse an absolute record or query time without depending on the host timezone."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps require a UTC offset")
    return result


def visible(value: Any) -> Any:
    """Retain recorded content while omitting hidden reasoning and encrypted payloads."""
    if isinstance(value, list):
        return [visible(item) for item in value if not isinstance(item, dict)
                or item.get("type") not in {"thinking", "redacted_thinking", "reasoning"}]
    if isinstance(value, dict):
        if value.get("type") == "encrypted_content":
            return {"type": "encrypted_content", "unavailable": True}
        return {key: visible(item) for key, item in value.items()
                if key not in {"encrypted_content", "encrypted_reasoning", "signature"}}
    return value


def content_text(value: Any) -> str:
    """Render one visible record consistently for index lengths and paged reads."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def has_encrypted_content(value: Any) -> bool:
    """Identify explicitly encrypted work evidence before selecting visible fields."""
    if isinstance(value, list):
        return any(has_encrypted_content(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"thinking", "redacted_thinking", "reasoning"}:
            return False
        return value.get("type") == "encrypted_content" or any(
            (key in {"encrypted_content", "encrypted_reasoning"} and bool(item))
            or has_encrypted_content(item) for key, item in value.items()
        )
    return False


class SessionRecords:
    """Own one read-only transcript snapshot, its work records and native call links."""

    def __init__(self, path: Path, through_line: int | None = None):
        """Read complete source lines, reusing a prior boundary for stable character paging."""
        self.path = path.resolve()
        self.metadata: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []
        self.unsupported: Counter[str] = Counter()
        self.partial_line: int | None = None
        self.through_line = 0
        self.calls: dict[str, dict[str, list[int]]] = defaultdict(
            lambda: {"calls": [], "results": []}
        )
        with self.path.open(encoding="utf-8") as stream:
            for line, raw in enumerate(stream, 1):
                if through_line is not None and line > through_line:
                    break
                if not raw.strip():
                    if not raw.endswith("\n"):
                        self.partial_line = line
                        break
                    self.through_line = line
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as error:
                    if not raw.endswith("\n"):
                        self.partial_line = line
                        break
                    raise ValueError(f"{self.path}:{line}: {error.msg}") from error
                if not isinstance(record, dict):
                    raise ValueError(  # noqa: TRY004 - Malformed transcript data is a CLI input error.
                        f"{self.path}:{line}: expected a JSON object"
                    )
                self._read(record, line)
                self.through_line = line
        if through_line is not None and through_line > self.through_line:
            raise ValueError(
                f"{self.path}: requested through line {through_line}, "
                f"but only {self.through_line} complete lines are available"
            )

    def _read(self, record: dict[str, Any], line: int) -> None:
        """Decode the two transcript layouts without counting native views as tool calls."""
        payload = record.get("payload", {})
        kind = record.get("type", "")
        if kind == "session_meta":
            # A fork can contain copied parent metadata immediately after its own.
            if not self.metadata:
                self.metadata = {key: payload[key] for key in (
                    "id", "session_id", "timestamp", "cwd", "source", "thread_source",
                    "parent_thread_id", "forked_from_id",
                ) if key in payload}
                self.metadata["format"] = "codex"
            return
        if kind == "response_item":
            self._codex_response(record, payload, line)
        elif kind == "event_msg" and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
            # Forks retain second-precision start times even when outer timestamps are rewritten.
            started = payload.get("started_at")
            created = self.metadata.get("timestamp")
            inherited = None
            if started is not None and created:
                created_second = int(timestamp(created).timestamp())
                if started != created_second:
                    inherited = started < created_second
            self._append(record, line, "lifecycle", payload, name=payload["type"], inherited=inherited)
        elif kind == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item", {})
            item_kind = item.get("type", "")
            if item_kind in {"Reasoning", "UserMessage", "AgentMessage"}:
                return
            supported = {
                "CommandExecution", "McpToolCall", "FileChange", "SubAgentActivity",
                "CollabAgentToolCall", "Extension", "ImageView", "ContextCompaction",
            }
            if item_kind not in supported:
                self.unsupported[f"codex.item.{item_kind}"] += 1
            self._append(record, line, "execution" if item_kind == "CommandExecution"
                         else "native_item", item, name=item_kind,
                         inherited=(payload["thread_id"] != self.metadata.get("id"))
                         if payload.get("thread_id") else None)
        elif kind in {"user", "assistant"} and "message" in record:
            self._claude_message(record, line)
        elif "tool" in kind or "call" in kind:
            self.unsupported[f"record.{kind}"] += 1
            self._append(record, line, "unsupported", record)

    def _codex_response(self, record: dict[str, Any], payload: dict[str, Any], line: int) -> None:
        """Preserve raw tool calls, results, visible messages and inter-agent messages."""
        kind = payload.get("type", "")
        if payload.get("role") in {"developer", "system"} or kind == "additional_tools":
            return
        if kind == "message" and payload.get("role") in {"user", "assistant"}:
            self._append(record, line, "message", {
                "role": payload["role"], "content": payload.get("content", []),
            })
        elif kind == "agent_message":
            self._append(record, line, "agent_message", {
                key: payload[key] for key in ("author", "recipient", "content") if key in payload
            })
        elif kind in {"function_call", "custom_tool_call"}:
            self._append(record, line, "tool_call", {
                key: payload[key] for key in ("type", "name", "namespace", "arguments", "input", "status")
                if key in payload
            }, name=payload.get("name"), calls=[payload["call_id"]] if payload.get("call_id") else [])
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            self._append(record, line, "tool_result", {
                "type": kind, "output": payload.get("output"),
            }, results=[payload["call_id"]] if payload.get("call_id") else [])
        elif "tool" in kind or "call" in kind:
            self.unsupported[f"codex.response.{kind}"] += 1
            self._append(record, line, "unsupported", {
                key: value for key, value in payload.items()
                if key != "internal_chat_message_metadata_passthrough"
            })

    def _claude_message(self, record: dict[str, Any], line: int) -> None:
        """Read physical Claude messages and link tool blocks by their native use IDs."""
        if not self.metadata:
            self.metadata = {"format": "claude", **{key: record[key] for key in (
                "sessionId", "cwd",
            ) if key in record}}
        message = record["message"]
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else []
        calls = [block["id"] for block in blocks
                 if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")]
        results = [block["tool_use_id"] for block in blocks
                   if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")]
        for block in blocks:
            if isinstance(block, dict) and "tool" in block.get("type", "") and block.get("type") not in {"tool_use", "tool_result"}:
                self.unsupported[f"claude.block.{block['type']}"] += 1
        cleaned = visible(content)
        if not cleaned:
            return
        self._append(record, line, "message", {
            "role": record["type"], "content": cleaned,
            **{key: record[key] for key in (
                "uuid", "parentUuid", "sessionId", "isSidechain", "agentId",
                "sourceToolAssistantUUID", "toolUseResult",
            ) if key in record},
        }, calls=calls, results=results)

    def _append(self, original: dict[str, Any], line: int, kind: str, content: Any, *,
                name: str | None = None, calls: Sequence[str] = (), results: Sequence[str] = (),
                inherited: bool | None = None) -> None:
        """Attach provenance and native call relations; leave missing results unresolved."""
        recorded_at = original.get("timestamp")
        meta = original.get("payload", {}).get("internal_chat_message_metadata_passthrough", {})
        created = meta.get("create_time")
        at = datetime.fromtimestamp(created, UTC).isoformat() if created is not None else recorded_at
        if inherited is None and created is not None and self.metadata.get("timestamp"):
            inherited = datetime.fromtimestamp(created, UTC) < timestamp(self.metadata["timestamp"])
        unavailable = has_encrypted_content(original.get("payload", original))
        content = visible(content)
        if unavailable:
            content = {**content, "unavailable_content": True}
        text = content_text(content)
        self.records.append({
            "line": line, "timestamp": at, "recorded_at": recorded_at,
            "kind": kind, "name": name, "inherited": inherited,
            "content": content, "call_ids": list(dict.fromkeys([*calls, *results])),
            "truncation_marker": bool(re.search(
                r"Warning: truncated output|…\d+ tokens truncated…|…\[truncated |Output truncated", text,
            )),
            "unavailable_content": unavailable,
        })
        for call_id in calls:
            self.calls[call_id]["calls"].append(line)
        for call_id in results:
            self.calls[call_id]["results"].append(line)

    def details(self, record: dict[str, Any]) -> dict[str, Any]:
        """Describe source locations and call pairing independently of rendered content."""
        pairs = {call_id: {**self.calls[call_id], "status": (
            "paired" if self.calls[call_id]["calls"] and self.calls[call_id]["results"]
            else "missing_call" if not self.calls[call_id]["calls"] else "no_recorded_result"
        )} for call_id in record["call_ids"]}
        return {"source": str(self.path), **{key: value for key, value in record.items()
                                               if key not in {"content", "call_ids"}},
                "calls": pairs}

    def index(self, args: argparse.Namespace) -> dict[str, Any]:
        """Return a bounded page with source cursors; filtering never changes the transcript."""
        selected = []
        omitted_inherited = 0
        for record in self.records:
            if record["line"] <= args.after_line:
                continue
            at = timestamp(record["timestamp"]) if record["timestamp"] else None
            if at is not None and ((args.since and at < args.since) or (args.until and at > args.until)):
                continue
            if record["inherited"] is True and not args.include_inherited:
                omitted_inherited += 1
                continue
            selected.append(record)
        page = selected[:args.limit]
        records = []
        for record in page:
            text = content_text(record["content"])
            details = {key: value for key, value in self.details(record).items()
                       if key not in {"source", "recorded_at"}}
            records.append({**details, "total_chars": len(text),
                            "preview": text[:160], "preview_truncated": len(text) > 160})
        return {"source": str(self.path), "session": self.metadata,
                "history": "physical JSONL records; identifiable inherited history is omitted unless requested",
                "omitted_inherited_records": omitted_inherited,
                "unsupported_work_kinds": dict(self.unsupported), "incomplete_final_line": self.partial_line,
                "through_line": self.through_line,
                "records": records, "more": len(selected) > len(page),
                "next_after_line": page[-1]["line"] if len(selected) > len(page) else None}

    def evidence_bundle(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return native call pairs and labeled intervening executions for one selected record."""
        pairs = self.details(record)["calls"]
        paired_lines = {line for pair in pairs.values() for key in ("calls", "results") for line in pair[key]}
        intervals = [(min(pair["calls"]), max(pair["results"])) for pair in pairs.values()
                     if pair["calls"] and pair["results"]]
        selected = [
            item for item in self.records
            if item["line"] == record["line"] or item["line"] in paired_lines
            or item["kind"] == "execution" and any(start < item["line"] < end for start, end in intervals)
        ]
        return {
            "selected_line": record["line"],
            "pairing_complete": all(pair["status"] == "paired" for pair in pairs.values()) if pairs else None,
            "unavailable_content": any(item["unavailable_content"] for item in selected),
            "truncation_marker": any(item["truncation_marker"] for item in selected),
            "interval_execution_meaning": "Recorded between a paired call and result; causation is not inferred.",
            "records": [
                {**self.details(item), "relationship": (
                    "selected" if item["line"] == record["line"] else
                    "paired" if item["line"] in paired_lines else "interval_execution"
                ), "content": item["content"]}
                for item in selected
            ],
        }

    def show(self, args: argparse.Namespace) -> dict[str, Any]:
        """Read one evidence bundle in character chunks without silently truncating it."""
        record = next((item for item in self.records if item["line"] == args.line), None)
        if record is None:
            raise ValueError(f"{self.path}:{args.line}: no visible work record")
        bundle = self.evidence_bundle(record)
        text = content_text(bundle)
        if args.offset > len(text):
            raise ValueError(f"Offset exceeds the evidence bundle's {len(text)} characters")
        end = min(args.offset + args.length, len(text))
        return {**self.details(record), **{key: bundle[key] for key in (
                    "pairing_complete", "unavailable_content", "truncation_marker",
                )}, "offset": args.offset, "end": end,
                "total_chars": len(text), "content": text[args.offset:end],
                "more": end < len(text), "next_offset": end if end < len(text) else None,
                "through_line": self.through_line,
                "incomplete_final_line": self.partial_line}


def nonnegative(value: str) -> int:
    """Accept a source cursor or character offset for the reader CLI."""
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer")
    return result


def positive(value: str) -> int:
    """Accept a nonzero page size or one-based source line."""
    result = nonnegative(value)
    if result == 0:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return result


def main() -> None:
    """Expose bounded indexing and exact record reads without running or resuming an agent."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="List visible work records and their original locations")
    index.add_argument("file", type=Path)
    index.add_argument("--since", type=timestamp)
    index.add_argument("--until", type=timestamp)
    index.add_argument("--after-line", type=nonnegative, default=0)
    index.add_argument("--limit", type=positive, default=20)
    index.add_argument("--include-inherited", action="store_true", help="Include identifiable copied parent history")
    show = commands.add_parser("show", help="Read one work record using character offsets")
    show.add_argument("file", type=Path)
    show.add_argument("line", type=positive)
    show.add_argument("--offset", type=nonnegative, default=0)
    show.add_argument("--length", type=positive, default=6000)
    show.add_argument("--through-line", type=positive, help="Source boundary returned by the first page")
    args = parser.parse_args()
    try:
        if args.command == "index" and args.since and args.until and args.since > args.until:
            raise ValueError("--since must not be later than --until")
        if args.command == "show" and args.offset and args.through_line is None:
            raise ValueError("Continue with --through-line from the first page alongside --offset")
        session = SessionRecords(args.file, getattr(args, "through_line", None))
        result = session.index(args) if args.command == "index" else session.show(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
