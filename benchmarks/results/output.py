# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish benchmark runs to the console, a local result directory, and Weights & Biases."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional, Protocol

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_benchmark_summary
from benchmarks.results.metrics import RequestMeasurement
from benchmarks.results.wandb import WandbBenchmarkRun

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkRun:
    """Result of one benchmark: its run record, aggregated metrics, per-request measurements, and artifacts.

    ``measurements`` is ``None`` when a composition publishes only aggregated
    points. ``artifacts`` maps a name to a file produced by the run, such as the
    raw trace replay records or a sweep's Pareto plot.
    """

    record: dict[str, Any]
    metrics: dict[str, Any]
    measurements: list[RequestMeasurement] | None
    artifacts: dict[str, Path]


class ResultSink(Protocol):
    """One result destination opened before the run executes and closed after publication."""

    def open(self, record: dict[str, Any]) -> None: ...

    def publish(self, run: BenchmarkRun) -> None: ...

    def close(self) -> None: ...


class ConsoleSink:
    """Print the run summary to the console log."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        log_benchmark_summary(run.record, run.metrics)

    def close(self) -> None:
        return None


class LocalDirectorySink:
    """Own the local result directory and the configuration and metrics JSON files in it."""

    def __init__(self, benchmark: BenchmarkConfig, output_dir: str) -> None:
        self.benchmark = benchmark
        self.output_dir = output_dir

    def open(self, record: dict[str, Any]) -> None:
        os.makedirs(self.output_dir, exist_ok=True)

    def publish(self, run: BenchmarkRun) -> None:
        write_json(
            self.output_dir,
            "config.json",
            {**self.benchmark.to_dict(), **run.record},
        )
        write_json(self.output_dir, "metrics.json", run.metrics)
        if run.measurements is not None and "raw_output" not in run.artifacts:
            write_json(self.output_dir, "raw_output.json", [
                {
                    "success": item.succeeded,
                    "status_code": item.status_code,
                    "error": item.error_message,
                    "stream": bool(run.metrics["stream"]),
                    "start_time": item.started_at,
                    "end_time": item.started_at + item.latency,
                    "latency": item.latency,
                    "ttft": item.ttft if run.metrics["stream"] else None,
                    "tpot": item.tpot if run.metrics["stream"] else None,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "inter_token_latencies": list(item.itl_samples) if run.metrics["stream"] else [],
                    "conversation_id": item.conversation_id,
                    "turn": item.turn,
                }
                for item in run.measurements
            ])
        logger.info("Results saved: %s", self.output_dir)

    def close(self) -> None:
        return None


class WandbSink:
    """Publish one run as an independent W&B run created when the sink opens."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        execution_dir: str,
        label: Optional[str],
        group: Optional[str],
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.execution_dir = execution_dir
        self.label = label
        self.group = group
        self.wandb_run = WandbBenchmarkRun()

    def open(self, record: dict[str, Any]) -> None:
        resolved = record["resolved"]
        self.wandb_run.start(
            self.benchmark,
            self.service,
            output_dir=self.execution_dir,
            parallel=int(resolved["parallel"]),
            rate=float(resolved["rate"]),
            name_suffix=self.label,
            group=self.group,
        )

    def publish(self, run: BenchmarkRun) -> None:
        raw_output = run.artifacts.get("raw_output")
        if raw_output is not None:
            self.wandb_run.log_trace_measurements(
                json.loads(raw_output.read_text(encoding="utf-8"))
            )
        self.wandb_run.log_metrics(run.metrics)

    def close(self) -> None:
        self.wandb_run.finish()


def result_directory_path(
    benchmark: BenchmarkConfig,
    output_dir: Optional[str] = None,
) -> str:
    """Return the explicit result directory or a new timestamped one under ``--output-dir``."""
    if output_dir is not None:
        return output_dir
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(benchmark.outputs.output_dir, timestamp)


def write_json(directory: str, filename: str, data: Any) -> Path:
    """Write one JSON artifact into a result directory and return its path."""
    path = Path(directory) / filename
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    return path


def resolved_load_record(benchmark: BenchmarkConfig) -> dict[str, Any]:
    """Map internal workload settings to scalar fields used by result consumers."""
    load = benchmark.load
    max_concurrency = int(load.max_concurrency)
    return {
        "parallel": max_concurrency,
        "number": int(load.request_count),
        "rate": float(load.arrival_rate),
        "open_loop": max_concurrency == -1,
        "resolved_parallel": max_concurrency,
    }


def build_benchmark_run_record(
    benchmark: BenchmarkConfig,
    service: ModelService,
    mode: str,
    load_record: dict[str, Any],
) -> dict[str, Any]:
    """Build the run record shared by console output and local artifacts."""
    workload = benchmark.resolved_workload
    record = {
        "mode": mode,
        "model": service.model,
        "url": service.chat_completions_url,
        "parallel": load_record["parallel"],
        "number": load_record["number"],
        "rate": load_record["rate"],
        "open_loop": load_record["open_loop"],
        "stream": benchmark.generation.stream,
        "resolved": {
            "parallel": load_record["resolved_parallel"],
            "number": load_record["number"],
            "rate": load_record["rate"],
        },
    }
    if benchmark.is_multi_turn:
        record["multi_turn"] = True
        record["max_turns"] = workload.max_turns
    if workload.dataset_selectors == ["random"]:
        record["random_seed"] = workload.random_seed
    return record


class ResultOutputs:
    """Open the sinks selected by ``--output`` around one run and own its execution directory.

    The execution directory is the local result directory when local output is
    enabled and a temporary directory otherwise; the engine writes its files
    there, and the temporary directory is removed when the context exits.
    """

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        record: dict[str, Any],
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.record = record
        self.label = label.strip() or None
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self._sinks: list[ResultSink] = []
        self._temporary_directory: TemporaryDirectory[str] | None = None
        self._execution_dir: str | None = None

    @property
    def execution_dir(self) -> str:
        """Return the local directory used by the benchmark engine for this run."""
        if self._execution_dir is None:
            raise RuntimeError("result outputs are not active")
        return self._execution_dir

    def __enter__(self) -> ResultOutputs:
        """Acquire the execution directory and open every selected sink."""
        if self._execution_dir is not None:
            raise RuntimeError("result outputs are already active")
        outputs = self.benchmark.outputs
        sinks: list[ResultSink] = []
        if not outputs.includes("quiet"):
            sinks.append(ConsoleSink())
        if outputs.includes("local"):
            local = LocalDirectorySink(
                self.benchmark,
                result_directory_path(self.benchmark, self.output_dir),
            )
            sinks.append(local)
            self._execution_dir = local.output_dir
        else:
            self._temporary_directory = TemporaryDirectory(
                prefix="foretoken-benchmark-"
            )
            self._execution_dir = self._temporary_directory.name
        if outputs.includes("wandb"):
            sinks.append(
                WandbSink(
                    self.benchmark,
                    self.service,
                    execution_dir=self._execution_dir,
                    label=self.label,
                    group=self.wandb_group,
                )
            )
        try:
            for sink in sinks:
                sink.open(self.record)
        except BaseException:
            self._cleanup()
            raise
        self._sinks = sinks
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Close every sink once and remove temporary execution files after success or failure."""
        try:
            for sink in self._sinks:
                try:
                    sink.close()
                except Exception:
                    if exc_type is None:
                        raise
                    logger.exception("Failed to close %s", type(sink).__name__)
        finally:
            self._sinks = []
            self._cleanup()
        return False

    def _cleanup(self) -> None:
        self._execution_dir = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def publish(self, run: BenchmarkRun) -> None:
        """Publish the finished run to every open sink before the owner closes resources."""
        for sink in self._sinks:
            sink.publish(run)
