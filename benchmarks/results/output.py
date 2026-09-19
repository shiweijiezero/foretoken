# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish benchmark runs to the console, a local result directory, and Weights & Biases."""

from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, Optional, Protocol

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_benchmark_summary
from benchmarks.results.environment import client_environment, serving_environment
from benchmarks.results.metrics import RequestMeasurement
from benchmarks.results.replicas import KubernetesReplicaObserver
from benchmarks.results.wandb import WandbBenchmarkRun
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkRun:
    """Result of one benchmark: its run record, aggregated metrics, per-request measurements, and artifacts.

    ``measurements`` is ``None`` when a composition publishes only aggregated
    points. ``artifacts`` maps a name to a file produced by the run. ``time_origin``
    is the monotonic clock value corresponding to elapsed time zero for observers.
    """

    record: dict[str, Any]
    metrics: dict[str, Any]
    measurements: list[RequestMeasurement] | None
    artifacts: dict[str, Path]
    time_origin: float | None = None


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
        replica_observations = None
        replica_path = run.artifacts.get("replica_observations")
        if replica_path is not None:
            replica_observations = json.loads(
                replica_path.read_text(encoding="utf-8")
            )
        if run.measurements is not None:
            self.wandb_run.log_request_history(
                run.measurements,
                duration=float(run.metrics["benchmark_time"]),
                stream=bool(run.metrics["stream"]),
                replica_observations=replica_observations,
            )
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
    """Return an explicit child path, or reserve a unique local directory under ``--output-dir``."""
    if output_dir is not None:
        return output_dir
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if benchmark.outputs.includes("local"):
        os.makedirs(benchmark.outputs.output_dir, exist_ok=True)
        return mkdtemp(prefix=f"{timestamp}-", dir=benchmark.outputs.output_dir)
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
        self._resources = ExitStack()
        self._execution_dir: str | None = None
        self._replica_observer: KubernetesReplicaObserver | None = None
        self._environment: dict[str, Any] | None = None

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
            self._execution_dir = self._resources.enter_context(
                TemporaryDirectory(prefix="foretoken-benchmark-")
            )
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
                self._resources.callback(sink.close)
                sink.open(self.record)
            if outputs.includes("local"):
                self._environment = {
                    "client": client_environment(),
                    "before": serving_environment(self.service),
                }
                write_json(self.execution_dir, "environment.json", self._environment)
            if self.service.model_service_refs and (
                outputs.includes("local") or outputs.includes("wandb")
            ):
                try:
                    observer = KubernetesReplicaObserver(
                        self.service.model_service_refs,
                        self.service.model,
                    )
                    observer.start()
                except (DeploymentError, RuntimeError) as exc:
                    logger.warning(
                        "Replica observation unavailable; continuing with benchmark: %s",
                        exc,
                    )
                else:
                    self._replica_observer = observer
                    self._resources.callback(self._close_replica_observer)
        except BaseException:
            self._resources.close()
            self._execution_dir = None
            raise
        self._sinks = sinks
        return self

    def _close_replica_observer(self) -> None:
        """Stop the observer once when publication or context cleanup takes ownership."""
        observer = self._replica_observer
        self._replica_observer = None
        if observer is not None:
            observer.close()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Close every sink once and remove temporary execution files after success or failure."""
        try:
            return self._resources.__exit__(exc_type, exc_value, traceback)
        finally:
            self._sinks = []
            self._execution_dir = None
            self._replica_observer = None

    def publish(self, run: BenchmarkRun) -> None:
        """Stop run observations, then publish the finished run to every open sink."""
        observer = self._replica_observer
        self._replica_observer = None
        if observer is not None:
            if run.time_origin is None:
                observer.close()
            else:
                observations = observer.finish(run.time_origin)
                if observations:
                    run.artifacts["replica_observations"] = write_json(
                        self.execution_dir,
                        "replica_observations.json",
                        observations,
                    )
        if self._environment is not None:
            self._environment["after"] = serving_environment(self.service)
            run.artifacts["environment"] = write_json(
                self.execution_dir, "environment.json", self._environment,
            )
        for sink in self._sinks:
            sink.publish(run)
