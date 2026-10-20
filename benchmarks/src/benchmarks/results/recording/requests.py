# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist and restore HTTP measurements independently of plotting or publication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.results.measurements.itl import ITLStatistics, ITLSummary
from benchmarks.results.measurements.requests import RequestMeasurement


class RequestRecordWriter:
    """Own a flushed JSONL journal so completed requests survive an interrupted run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = path.open("w", encoding="utf-8", buffering=1)

    def write(self, record: dict[str, Any]) -> None:
        """Append a completed request without retaining request or response content."""
        self.file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.file.flush()

    def close(self) -> None:
        """Close the journal after the owning result lifecycle finishes publication."""
        self.file.close()


def read_request_records(directory: Path, name: str = "raw_output") -> list[dict[str, Any]]:
    """Read published requests or their flushed journal for offline result consumers."""
    published = directory / f"{name}.json"
    if published.is_file():
        return json.loads(published.read_text(encoding="utf-8"))
    journal = directory / f"{name}.jsonl"
    if journal.is_file():
        with journal.open(encoding="utf-8") as file:
            return sorted((json.loads(line) for line in file if line.strip()), key=lambda row: row["start_time"])
    return []


def request_measurement_record(item: RequestMeasurement, *, stream: bool) -> dict[str, Any]:
    """Serialize a request on the elapsed measurement clock for HTTP and trace result readers."""
    return {
        "success": item.succeeded,
        "status_code": item.status_code,
        "error": item.error_message,
        "stream": stream,
        "start_time": item.started_at,
        "end_time": item.started_at + item.latency,
        "latency": item.latency,
        "ttft": item.ttft if stream else None,
        "tpot": item.tpot if stream else None,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "cached_input_tokens": item.cached_input_tokens,
        "itl_summary": item.itl_summary,
        "request_id": item.request_id,
        "scheduled_at": item.scheduled_at,
        "send_delay": item.send_delay,
        "phase": item.phase,
        "source_id": item.source_id,
        "finish_reason": item.finish_reason,
        "response_id": item.response_id,
        "response_model": item.response_model,
        "content_characters": item.content_characters,
        "reasoning_characters": item.reasoning_characters,
        "transport_completed": item.transport_completed,
        "conversation_id": item.conversation_id,
        "turn": item.turn,
        "dataset": item.dataset,
        "model": item.model,
        "priority": item.priority,
        "request_class": item.request_class,
        "target_output_tokens": item.target_output_tokens,
    }


def load_http_measurements(raw: list[dict[str, Any]]) -> list[RequestMeasurement]:
    """Restore elapsed HTTP observations for result recovery and chart consumers."""
    return [
        RequestMeasurement(
            started_at=float(row["start_time"]),
            ttft=row.get("ttft"),
            latency=float(row["latency"]),
            tpot=row.get("tpot"),
            itl_summary=_saved_itl_summary(row),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cached_input_tokens=row.get("cached_input_tokens"),
            succeeded=bool(row["success"]),
            conversation_id=row.get("conversation_id"),
            turn=row.get("turn"),
            status_code=row.get("status_code"),
            error_message=row.get("error"),
            dataset=row.get("dataset"),
            model=row.get("model"),
            priority=row.get("priority"),
            request_class=row.get("request_class"),
            target_output_tokens=row.get("target_output_tokens"),
            request_id=row.get("request_id"),
            phase=row.get("phase"),
            source_id=row.get("source_id"),
            finish_reason=row.get("finish_reason"),
            response_id=row.get("response_id"),
            response_model=row.get("response_model"),
            content_characters=row.get("content_characters"),
            reasoning_characters=row.get("reasoning_characters"),
            transport_completed=row.get("transport_completed"),
            scheduled_at=row.get("scheduled_at"),
            send_delay=row.get("send_delay"),
        )
        for row in raw
    ]


def _saved_itl_summary(row: dict[str, Any]) -> ITLSummary:
    """Read scalar ITL records, reducing legacy interval arrays at the file boundary."""
    if "itl_summary" in row:
        return row["itl_summary"]
    statistics = ITLStatistics()
    for interval in row.get("inter_token_latencies") or ():
        statistics.observe(interval)
    return statistics.summary()
