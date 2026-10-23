# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Persist and restore HTTP measurements independently of plotting or publication."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from benchmarks.results.measurements.itl import ITLStatistics, ITLSummary
from benchmarks.results.measurements.requests import RequestMeasurement
from benchmarks.results.recording.database import RunRecords, read_recorded_requests
from benchmarks.results.recording.files import write_json


class RequestRecordWriter:
    """Commit completed requests and own their live JSONL export for one measurement channel."""

    def __init__(self, path: Path, records: RunRecords) -> None:
        """Open a fresh channel, borrowing the database owned by ResultOutputs."""
        self.path = path
        self.records = records
        self.channel = path.stem
        records.start_requests(self.channel)
        self.file = path.open("w", encoding="utf-8", buffering=1)
        self._executor: ThreadPoolExecutor | None = None
        self._failure: BaseException | None = None

    def write(self, record: dict[str, Any]) -> None:
        """Persist a completion from synchronous evaluator progress or the owned I/O worker."""
        try:
            self.file.write(self.records.append_request(self.channel, record) + "\n")
            self.file.flush()
        except BaseException as error:
            self._failure = error
            raise

    async def awrite(self, record: dict[str, Any]) -> None:
        """Await durable recording without blocking HTTP/video token delivery on filesystem I/O."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark-records")
        pending = asyncio.wrap_future(self._executor.submit(self.write, record))
        # Cancellation leaves the accepted write owned by close(). Its error must
        # still surface there if the request no longer awaits the future.
        pending.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        await asyncio.shield(pending)

    def close(self) -> None:
        """Join accepted writes before closing the journal or returning database ownership to ResultOutputs."""
        try:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
        finally:
            self.file.close()
        if self._failure is not None:
            raise self._failure


def read_request_records(directory: Path, name: str = "raw_output") -> list[dict[str, Any]]:
    """Restore elapsed HTTP measurements in send order for recovery and offline readers."""
    return sorted(read_recorded_requests(directory, name), key=lambda row: row["start_time"])


def export_http_requests(
    directory: Path, metrics: dict[str, Any], *, name: str = "raw_output", traced: bool = False,
) -> Path:
    """Project retained HTTP observations and measured SLO results into the public JSON artifact."""
    raw = read_request_records(directory, name)
    if len(raw) != metrics["request_num"]:
        raise ValueError(f"Cannot export completed measurement: request records are incomplete in {directory}")
    slo = metrics.get("slo") or {}
    met = slo.get("request_slo_met")
    rows = []
    for index, (row, measurement) in enumerate(zip(raw, load_http_measurements(raw))):
        record = {
            **(row if traced else {}),
            **request_measurement_record(measurement, stream=bool(metrics["stream"])),
        }
        if isinstance(met, list):
            record.update(slo_met=met[index], slo_target=slo["request_criteria"])
        rows.append(record)
    return write_json(str(directory), f"{name}.json", rows, compact=True)


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
