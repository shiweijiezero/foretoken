# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse StudyChat and Mooncake arrival traces into time-ordered request events."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.datasets.conversations import Task, Turn, iter_dataset_rows

# Each Mooncake hash ID identifies one fixed-size input-token block.
MOONCAKE_BLOCK_TOKENS = 512

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArrivalTraceEvent:
    """Store a request arrival time, source row identity, and optional bound request task."""

    timestamp_seconds: float
    source_row_index: int
    request: Task | None = None
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
    turns: list[Turn] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not {"role", "content"} <= message.keys():
            raise ValueError(
                f"Invalid message {index} at {dataset_path}:{line_number}"
            )
        turns.append(
            Turn(
                role=str(message["role"]),
                content=message["content"],
                extra={
                    key: value
                    for key, value in message.items()
                    if key not in {"role", "content"}
                },
            )
        )

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
        request=Task(id=f"{dataset_path}:{source_row_index}", turns=tuple(turns)),
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

    def _iter_rows(self) -> Iterator[tuple[Path, int, int, Any]]:
        """Yield trace rows through the shared local and Hub source reader."""
        if not Path(self.trace_selector).expanduser().is_file():
            logger.info("Resolving trace source %s", self.trace_selector)
        yield from iter_dataset_rows(self.trace_selector)

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
