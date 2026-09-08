# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse StudyChat and Mooncake data into HTTP request arrival events."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.performance.chat_client import ChatRequestContent
from benchmarks.performance.huggingface_datasets import (
    is_hf_dataset_spec,
    iter_hf_rows,
    resolve_hf_file_uri,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArrivalTraceEvent:
    """Store a request arrival time, source row identity, and optional chat content."""

    timestamp_seconds: float
    source_row_index: int
    request: ChatRequestContent | None = None
    input_tokens: int | None = None
    hash_ids: list[int] | None = None
    conversation_id: str | None = None
    request_origin: str = ""


def _parse_studychat_event(
    row: object,
    *,
    dataset_path: Path,
    line_number: int,
    source_row_index: int,
) -> ArrivalTraceEvent:
    """Parse one StudyChat row while preserving its complete message context."""
    if not isinstance(row, dict):
        raise ValueError(f"Expected an object at {dataset_path}:{line_number}")

    required = ("timestamp", "chatId", "messages")
    missing = [name for name in required if name not in row]
    if missing:
        raise ValueError(
            f"Missing {', '.join(missing)} at {dataset_path}:{line_number}"
        )

    try:
        timestamp_ms = float(row["timestamp"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid timestamp at {dataset_path}:{line_number}"
        ) from error
    if not math.isfinite(timestamp_ms):
        raise ValueError(
            f"Timestamp must be finite at {dataset_path}:{line_number}"
        )

    conversation_id = row["chatId"]
    if conversation_id is None or not str(conversation_id):
        raise ValueError(f"Empty chatId at {dataset_path}:{line_number}")

    messages = row["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Invalid messages at {dataset_path}:{line_number}")

    input_tokens = row.get("input_length")
    if input_tokens is not None:
        try:
            input_tokens = int(input_tokens)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid input_length at {dataset_path}:{line_number}"
            ) from error
        if input_tokens <= 0:
            raise ValueError(
                f"Invalid input_length at {dataset_path}:{line_number}"
            )

    return ArrivalTraceEvent(
        timestamp_seconds=timestamp_ms / 1000.0,
        source_row_index=source_row_index,
        request=ChatRequestContent(messages=messages),
        input_tokens=input_tokens,
        conversation_id=str(conversation_id),
    )


def _parse_mooncake_event(
    row: object,
    *,
    dataset_path: Path,
    line_number: int,
    source_row_index: int,
) -> ArrivalTraceEvent:
    """Parse one Mooncake row without constructing request text at this stage."""
    if not isinstance(row, dict):
        raise ValueError(f"Expected an object at {dataset_path}:{line_number}")
    if "timestamp" not in row or "input_length" not in row:
        raise ValueError(
            "Mooncake trace needs timestamp and input_length at "
            f"{dataset_path}:{line_number}"
        )
    try:
        timestamp_ms = float(row["timestamp"])
        input_tokens = int(row["input_length"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid timestamp or input_length at {dataset_path}:{line_number}"
        ) from error
    if not math.isfinite(timestamp_ms) or input_tokens <= 0:
        raise ValueError(
            f"Invalid timestamp or input_length at {dataset_path}:{line_number}"
        )

    hash_ids = row.get("hash_ids")
    if hash_ids is not None:
        if not isinstance(hash_ids, list) or any(
            isinstance(hash_id, bool)
            or not isinstance(hash_id, int)
            or hash_id < 0
            for hash_id in hash_ids
        ):
            raise ValueError(f"Invalid hash_ids at {dataset_path}:{line_number}")

    conversation_id = row.get("chatId")
    return ArrivalTraceEvent(
        timestamp_seconds=timestamp_ms / 1000.0,
        source_row_index=source_row_index,
        input_tokens=input_tokens,
        hash_ids=hash_ids,
        conversation_id=(
            str(conversation_id) if conversation_id is not None else None
        ),
    )


TraceRowParser = Callable[..., ArrivalTraceEvent]
_TRACE_ROW_PARSERS: dict[str, TraceRowParser] = {
    "studychat": _parse_studychat_event,
    "mooncake": _parse_mooncake_event,
}


class ArrivalTraceReader:
    """Own trace resolution, format detection, time-window selection, and stable sorting."""

    def __init__(self, trace_selector: str | Path) -> None:
        self.trace_selector = str(trace_selector)
        self.trace_format: str | None = None

    def _resolve_local_path(self) -> Path:
        local_path = Path(self.trace_selector).expanduser()
        if local_path.exists():
            return local_path
        if not self.trace_selector.startswith("hf://"):
            return local_path

        logger.info("Resolving Hugging Face trace %s", self.trace_selector)
        return Path(resolve_hf_file_uri(self.trace_selector))

    def _iter_rows(self) -> Iterator[tuple[Path, int, int, Any]]:
        local_path = Path(self.trace_selector).expanduser()
        is_huggingface_dataset = (
            not local_path.exists()
            and not self.trace_selector.startswith("hf://")
            and is_hf_dataset_spec(self.trace_selector)
        )
        if is_huggingface_dataset:
            dataset_label = Path(f"hf://{self.trace_selector}")
            logger.info("Resolving Hugging Face trace %s", self.trace_selector)
            for row_index, row in iter_hf_rows(self.trace_selector):
                yield dataset_label, row_index + 1, row_index, row
            return

        dataset_path = self._resolve_local_path()
        if not dataset_path.is_file():
            raise FileNotFoundError(f"Trace JSONL not found: {dataset_path}")
        source_row_index = 0
        with dataset_path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    row: Any = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid JSON at {dataset_path}:{line_number}: {error}"
                    ) from error
                yield dataset_path, line_number, source_row_index, row
                source_row_index += 1

    def _iter_events(self) -> Iterator[ArrivalTraceEvent]:
        row_parser: TraceRowParser | None = None
        for dataset_path, line_number, source_row_index, row in self._iter_rows():
            if row_parser is None:
                trace_format = self._detect_format(
                    row,
                    dataset_path=dataset_path,
                    line_number=line_number,
                )
                if self.trace_format not in (None, trace_format):
                    raise ValueError("Trace format changed between reads")
                self.trace_format = trace_format
                row_parser = _TRACE_ROW_PARSERS[trace_format]
            yield row_parser(
                row,
                dataset_path=dataset_path,
                line_number=line_number,
                source_row_index=source_row_index,
            )

    @staticmethod
    def _detect_format(
        row: object,
        *,
        dataset_path: Path,
        line_number: int,
    ) -> str:
        if isinstance(row, dict):
            if all(key in row for key in ("timestamp", "chatId", "messages")):
                return "studychat"
            if all(key in row for key in ("timestamp", "input_length")):
                return "mooncake"
        raise ValueError(
            f"Cannot detect trace format from {dataset_path}:{line_number}; "
            "expected StudyChat timestamp/chatId/messages or Mooncake "
            "timestamp/input_length"
        )

    def read_window(
        self,
        *,
        start_offset_seconds: float = 0.0,
        duration_seconds: float | None = None,
    ) -> tuple[float, list[ArrivalTraceEvent]]:
        """Select a half-open window relative to the first timestamp and sort by arrival time."""
        self.trace_format = None
        first_timestamp = None
        for event in self._iter_events():
            if first_timestamp is None:
                first_timestamp = event.timestamp_seconds
            else:
                first_timestamp = min(first_timestamp, event.timestamp_seconds)

        if first_timestamp is None:
            raise ValueError("Trace contains no requests")

        window_start = first_timestamp + start_offset_seconds
        window_end = (
            None if duration_seconds is None else window_start + duration_seconds
        )
        selected_events = [
            event
            for event in self._iter_events()
            if event.timestamp_seconds >= window_start
            and (window_end is None or event.timestamp_seconds < window_end)
        ]
        if not selected_events:
            raise ValueError("Trace window contains no requests")
        selected_events.sort(key=lambda event: event.timestamp_seconds)
        return window_start, selected_events
