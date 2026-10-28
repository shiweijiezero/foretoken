# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own the result directory, measurement evidence, destinations, and cleanup for one run."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Self

import wandb
from foretoken.diagnostics import console_output_enabled
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from loguru import logger
from tqdm import tqdm

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.measurements.progress import HttpProgress
from benchmarks.results.measurements.requests import (
    RequestMeasurement,
    summarize_measurements,
)
from benchmarks.results.observations.environment import (
    OBSERVATION_TIMEOUT_SECONDS,
    ApplicationSources,
    client_environment,
    serving_environment,
)
from benchmarks.results.observations.prometheus import PrometheusObserver
from benchmarks.results.observations.replicas import KubernetesReplicaObserver
from benchmarks.results.recording.database import RunRecords
from benchmarks.results.recording.files import (
    result_directory_path,
    write_gpu_allocation_csv,
    write_json,
)
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.logs import capture_run_logs, terminal_stream
from benchmarks.results.recording.requests import (
    RequestRecordWriter,
    export_http_requests,
    request_measurement_record,
)
from benchmarks.results.recording.responses import ResponseWriter
from benchmarks.results.reporting.http import http_result_sinks
from benchmarks.results.reporting.plots import PlotSink
from benchmarks.results.types import (
    BenchmarkRun,
    ResultConfiguration,
    ResultSink,
    _ResultSinkFactory,
)
from benchmarks.results.wandb.recording import WandbSink


class ResultOutputs:
    """Own preparation logs, measurement observers, publication, and cleanup for one run.

    File outputs and W&B synchronization retain the execution directory.
    Console-only execution uses a temporary directory, removed after success
    and retained when local recording or benchmark execution fails.
    """

    def __init__(
        self,
        benchmark: ResultConfiguration,
        service: ModelService | None,
        *,
        label: str = "",
        output_dir: str | None = None,
        wandb_group: str | None = None,
        directory_prefix: str = "",
        sink_factory: _ResultSinkFactory | None = None,
    ) -> None:
        """Retain one run's inputs; entering the context acquires its recording resources."""
        self.benchmark = benchmark
        self.service = service
        self.label = label.strip() or None
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self.directory_prefix = directory_prefix
        self.sink_factory = sink_factory
        self._sinks: list[ResultSink] = []
        self._resources = ExitStack()
        self._execution_dir: str | None = None
        self._temporary_execution_dir = False
        self._replica_observer: KubernetesReplicaObserver | None = None
        self._prometheus_observer: PrometheusObserver | None = None
        self._environment: dict[str, Any] | None = None
        self._applications: ApplicationSources
        self._exit_code = 0
        self._context: dict[str, Any] = {}
        self._publication_lock = None
        self._records: RunRecords | None = None
        self._request_writers: list[RequestRecordWriter] = []
        self._http_progress: tqdm | None = None

    def __enter__(self) -> Self:
        """Acquire the execution directory and capture preparation through publication."""
        if self._execution_dir is not None:
            raise RuntimeError("result outputs are already active")
        outputs = self.benchmark.outputs
        if outputs.saves_local:
            self._execution_dir = result_directory_path(
                self.benchmark,
                self.output_dir,
                self.directory_prefix,
            )
        else:
            self._temporary_execution_dir = True
            self._execution_dir = mkdtemp(prefix="foretoken-benchmark-")
        self._execution_dir = str(Path(self._execution_dir).resolve())
        self._applications = ApplicationSources(Path(self.execution_dir))

        try:
            os.makedirs(self.execution_dir, exist_ok=True)
            lock = publication_lock(Path(self.execution_dir))
            lock.__enter__()
            self._publication_lock = lock
            self._records = RunRecords(Path(self.execution_dir))
            self._context = {
                "status": "running", "phase": "preparation", "exit_code": None,
                "measurement_exit_code": None,
                "started_at": datetime.now(UTC).isoformat(), "finished_at": None,
            }
            self.records.publish({"context.json": self._context})
            self._resources.enter_context(
                capture_run_logs(self.execution_dir, quiet=outputs.includes("quiet"))
            )
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def open(
        self, record: dict[str, Any], *, start_observers: bool = True,
        configuration: dict[str, Any] | None = None,
    ) -> None:
        """Retain the executed plan and open destinations; observation may begin after warmup."""
        directory = self.execution_dir
        self.records.publish({"config.json": configuration if configuration is not None else {
            **self.benchmark.to_dict(), **record,
        }})
        outputs = self.benchmark.outputs
        if self.sink_factory is not None:
            sinks = self.sink_factory(directory)
        else:
            if not isinstance(self.benchmark, BenchmarkConfig):
                raise TypeError("non-standard benchmark results require a sink factory")
            if self.service is None:
                raise TypeError("standard benchmark results require a service")
            sinks = http_result_sinks(
                self.benchmark, self.service, directory,
                label=self.label, wandb_group=self.wandb_group,
            )
        # Artifacts must exist before plotting, and plots before a remote destination consumes them.
        if outputs.includes("plot"):
            index = next((index for index, sink in enumerate(sinks) if isinstance(sink, WandbSink)), len(sinks))
            sinks.insert(index, PlotSink(directory))
        for sink in sinks:
            self._resources.callback(self._close_sink, sink)
            sink.open(record)
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        if outputs.saves_local:
            self._environment = {"client": client_environment(deadline=deadline)}
            write_json(self.execution_dir, "environment.json", self._environment)
        self._sinks = sinks
        if start_observers:
            self.start_observers(deadline=deadline)

    def publish(self, run: BenchmarkRun) -> None:
        """Save completed measurements before bounded observation finalization and presentation."""
        self.save_measurement(run)
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        # Stop both samplers before joining either. Their in-flight reads and
        # final snapshots share this deadline rather than starting fresh budgets.
        if self._replica_observer is not None:
            self._replica_observer.stop(deadline)
        if self._prometheus_observer is not None:
            self._prometheus_observer.stop(deadline)
        for name in ("warmup_raw_output", "warmup_metrics"):
            path = Path(self.execution_dir) / f"{name}.json"
            if path.is_file():
                run.artifacts[name] = path
        if self.benchmark.outputs.includes("quiet") and run.measurements is not None and run.metrics["failed_num"]:
            logger.error(
                "{}/{} requests failed; see {}",
                run.metrics["failed_num"], run.metrics["request_num"],
                self.execution_dir if self.benchmark.outputs.saves_local else "W&B result artifacts",
            )
        observer = self._replica_observer
        self._replica_observer = None
        if observer is not None:
            if run.time_origin is None:
                observer.close(deadline=deadline)
            else:
                observations = observer.finish(
                    run.time_origin, float(run.metrics["benchmark_time"]), deadline=deadline,
                )
                if self._environment is not None:
                    self._environment["workloads"] = observer.workload_history(
                        run.time_origin, float(run.metrics["benchmark_time"]),
                    )
                allocation = observer.gpu_allocation
                if allocation is not None:
                    run.metrics["gpu_allocation"] = {
                        key: value for key, value in allocation.items()
                        if key not in {"samples", "failed_read_elapsed_s"}
                    }
                    run.artifacts["gpu_allocation"] = write_json(
                        self.execution_dir, "gpu_allocation.json", allocation,
                    )
                    run.artifacts["gpu_allocation_csv"] = write_gpu_allocation_csv(
                        self.execution_dir, allocation,
                    )
                if observations:
                    run.artifacts["replica_observations"] = write_json(
                        self.execution_dir, "replica_observations.json", observations,
                    )
        prometheus_observer = self._prometheus_observer
        self._prometheus_observer = None
        if prometheus_observer is not None:
            observations = prometheus_observer.finish(
                run.time_origin, run.metrics.get("benchmark_time"), deadline=deadline,
            )
            if observations["resources"] is not None:
                run.metrics["resources"] = {
                    key: value for key, value in observations["resources"].items()
                    if key not in {"queries", "window_end_unix_seconds"}
                }
            if observations["speculative_decoding"] is not None:
                run.metrics["speculative_decoding"] = {
                    key: value for key, value in observations["speculative_decoding"].items()
                    if key not in {"queries", "window_end_unix_seconds"}
                }
            run.artifacts["prometheus_observations"] = write_json(
                self.execution_dir, "prometheus_observations.json", observations,
            )
        if self._environment is not None:
            if self.service is not None:
                self._environment["after"] = serving_environment(self.service, deadline=deadline)
            if "after" in self._environment:
                self._applications.capture(
                    self._environment["after"], kubectl=self.service.kubectl, deadline=deadline,
                )
            self._applications.save_archives(deadline=deadline)
            if self._applications.records:
                self._environment["applications"] = self._applications.records
            run.artifacts["environment"] = write_json(
                self.execution_dir, "environment.json", self._environment,
            )
        # Destination failures must leave the final measurements and publication inputs reusable.
        self.records.publish({
            "metrics.json": run.metrics,
            "publication.json": self._publication_record(run),
        })
        for sink in self._sinks:
            sink.publish(run)
            self.records.publish({"publication.json": self._publication_record(run)})

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Close every sink once without replacing an active benchmark failure."""
        error = exc_value
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            self._exit_code = 130
        elif isinstance(error, SystemExit):
            self._exit_code = error.code if isinstance(error.code, int) else int(error.code is not None)
        elif error is not None:
            self._exit_code = 1
        if error is not None:
            logger.opt(exception=(exc_type, exc_value, traceback)).debug("Benchmark stopped")
        try:
            try:
                deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
                try:
                    if self._replica_observer is not None:
                        self._replica_observer.stop(deadline)
                    if self._prometheus_observer is not None:
                        self._prometheus_observer.stop(deadline)
                finally:
                    self._resources.__exit__(exc_type, exc_value, traceback)
            except BaseException as cleanup_error:
                if error is not None:
                    logger.exception("Result cleanup also failed")
                else:
                    error = cleanup_error
                    self._exit_code = 130 if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) else 1
                    raise
        finally:
            self._context.update(
                status="interrupted" if self._exit_code == 130 else "failed" if self._exit_code else "completed",
                exit_code=self._exit_code, finished_at=datetime.now(UTC).isoformat(),
            )
            if error is not None:
                self._context["error_type"] = type(error).__name__
            try:
                if self._records is not None:
                    self._records.publish({"context.json": self._context})
            except (OSError, sqlite3.Error):
                if error is None:
                    self._exit_code = 1
                    raise
                logger.exception("Could not retain final run context")
            finally:
                try:
                    if self._records is not None:
                        self._records.close()
                finally:
                    self._records = None
                    if self._publication_lock is not None:
                        self._publication_lock.__exit__(None, None, None)
                        self._publication_lock = None
                    self._release_execution_directory(preserve=self._exit_code != 0)
                    self._sinks = []
                    self._execution_dir = None
                    self._replica_observer = None
                    self._prometheus_observer = None
                    self._request_writers = []
                    self._exit_code = 0
        return False

    @property
    def execution_dir(self) -> str:
        """Return the local directory used by the benchmark engine for this run."""
        if self._execution_dir is None:
            raise RuntimeError("result outputs are not active")
        return self._execution_dir

    @property
    def records(self) -> RunRecords:
        """Return this active run's record connection without transferring its lifetime."""
        if self._records is None:
            raise RuntimeError("result outputs are not active")
        return self._records

    def start_measurement(self) -> None:
        """Record actual execution or checkpoint reuse before the leaf runner begins measurement."""
        if self._context["phase"] == "preparation":
            self._context["phase"] = "measurement"
            self.records.publish({"context.json": self._context})

    def save_measurement(self, run: BenchmarkRun) -> None:
        """Commit metrics, publication inputs and the measured outcome as one checkpoint."""
        for writer in self._request_writers:
            writer.flush()
        self._close_http_progress()
        self._exit_code = run.exit_code if run.exit_code is not None else int(run.metrics["success_num"] == 0)
        self._context.update(phase="finalization", measurement_exit_code=self._exit_code)
        directory = Path(self.execution_dir)
        run.artifacts.update(config=directory / "config.json", metrics=directory / "metrics.json")
        if run.measurements is not None:
            run.artifacts["raw_output"] = directory / "raw_output.json"
        self.records.publish({
            "metrics.json": run.metrics,
            "publication.json": self._publication_record(run),
            "context.json": self._context,
        })
        if run.measurements is not None:
            export_http_requests(directory, run.metrics, traced=run.record.get("mode") == "arrival_trace")
        elif run.record.get("mode") == "video_generation":
            write_json(self.execution_dir, "raw_results.json", sorted(
                self.records.requests("raw_results"), key=lambda row: row["index"],
            ))

    def publish_progress(self, row: dict[str, Any], *, axis: str) -> None:
        """Send a measured progress snapshot to each selected live destination."""
        for sink in self._sinks:
            if isinstance(sink, WandbSink):
                sink.log_progress(row, axis=axis)

    def create_response_writer(self) -> ResponseWriter | None:
        """Open optional content evidence under this run and close it with the result lifecycle."""
        if not self.benchmark.outputs.includes("raw"):
            return None
        writer = ResponseWriter(self.execution_dir)
        self._resources.callback(writer.close)
        return writer

    def create_request_writer(self, name: str) -> RequestRecordWriter:
        """Open a completed-request journal and close it with this run's result resources."""
        writer = RequestRecordWriter(Path(self.execution_dir) / f"{name}.jsonl", self.records)
        self._request_writers.append(writer)
        self._resources.callback(writer.close)
        return writer

    def http_progress(
        self, *, stream: bool, total: int | None = None, criteria: dict[str, str] | None = None,
    ) -> HttpProgress | None:
        """Display actual HTTP completions and connect the optional live W&B destination."""
        if console_output_enabled() and terminal_stream(sys.stderr).isatty():
            self._http_progress = tqdm(
                total=total, desc="Completed requests", unit="request",
                file=terminal_stream(sys.stderr), disable=None, dynamic_ncols=True,
                mininterval=1.0, leave=False,
            )
            self._resources.callback(self._close_http_progress)
        if self.benchmark.outputs.includes("wandb"):
            return HttpProgress(self.publish_progress, stream=stream, criteria=criteria)
        return None

    def http_request_completed(self) -> None:
        """Advance the terminal after the request recorder accepts a measured completion."""
        if self._http_progress is not None:
            self._http_progress.update(1)

    def _close_http_progress(self) -> None:
        """Release the terminal before final tables or a failure are reported."""
        progress, self._http_progress = self._http_progress, None
        if progress is not None:
            progress.close()

    def capture_source_evidence(
        self, snapshot: dict[str, Any], *, kubectl: Kubectl | None,
        deadline: float | None = None,
    ) -> None:
        """Retain a live service's provenance and archives with its selected Kubernetes client."""
        if not self.benchmark.outputs.saves_local:
            return
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        self._applications.capture(snapshot, kubectl=kubectl, deadline=deadline)
        self._applications.save_archives(deadline=deadline)

    def start_observers(self, *, deadline: float | None = None) -> None:
        """Bound provenance preparation, then start each sampler with its own read budget."""
        outputs = self.benchmark.outputs
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        if self._environment is not None and self.service is not None:
            self._environment["before"] = serving_environment(self.service, deadline=deadline)
            self.capture_source_evidence(
                self._environment["before"], kubectl=self.service.kubectl, deadline=deadline,
            )
            write_json(self.execution_dir, "environment.json", self._environment)
        if self.service is not None and self.service.model_service_refs and outputs.saves_local:
            # Discovery budgets belong to each sampler, independently of provenance
            # preparation and the other sampler's initial read.
            try:
                observer = KubernetesReplicaObserver(
                    self.service.model_service_refs, self.service.model, self._applications,
                    kubectl=self.service.kubectl,
                )
                observer.start()
            except (DeploymentError, RuntimeError) as exc:
                logger.warning("Replica observation unavailable; continuing with benchmark: {}", exc)
            else:
                self._replica_observer = observer
                self._resources.callback(self._close_replica_observer)
            if self._replica_observer is None:
                return
            try:
                prometheus_observer = PrometheusObserver(self.service, self._replica_observer)
                prometheus_observer.start()
            except (DeploymentError, RuntimeError) as exc:
                logger.warning("Prometheus observation unavailable; continuing with benchmark: {}", exc)
            else:
                self._prometheus_observer = prometheus_observer
                self._resources.callback(self._close_prometheus_observer)

    def record_warmup(self, rows: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
        """Persist the completed warmup before measurement or a warmup failure."""
        for writer in self._request_writers:
            writer.flush()
        self.records.publish({"warmup_metrics.json": metrics})
        write_json(self.execution_dir, "warmup_raw_output.json", rows, compact=True)

    def record_http_warmup(
        self,
        measurements: list[RequestMeasurement],
        *,
        duration: float,
        stream: bool,
        arrival_rate: float,
        concurrency: int,
        itl_summary: dict[str, Any] | None = None,
    ) -> None:
        """Summarize and serialize HTTP warmup on its own clock, apart from measured results."""
        metrics = summarize_measurements(
            measurements, total_time=duration, stream=stream,
            arrival_rate=arrival_rate, request_count=len(measurements),
            reported_concurrency=concurrency, gpu_count=None,
            include_normalized_throughput=False, itl_summary=itl_summary,
        )
        self.record_warmup(
            [request_measurement_record(item, stream=stream) for item in measurements], metrics,
        )

    def _close_sink(self, sink: ResultSink) -> None:
        """Close one sink with the exit status owned by this result lifecycle."""
        sink.close(exit_code=self._exit_code)

    def _close_replica_observer(self) -> None:
        """Stop the observer once when publication or context cleanup takes ownership."""
        observer = self._replica_observer
        self._replica_observer = None
        if observer is not None:
            observer.close()

    def _close_prometheus_observer(self) -> None:
        """Stop the Prometheus observer once when cleanup takes ownership."""
        observer = self._prometheus_observer
        self._prometheus_observer = None
        if observer is not None:
            observer.close()

    def _release_execution_directory(self, *, preserve: bool) -> None:
        """Remove temporary files after success or report the directory retained after failure."""
        directory = self._execution_dir
        if directory is None:
            return
        if preserve:
            logger.error("Benchmark artifacts preserved: {}", directory)
        elif self._temporary_execution_dir:
            shutil.rmtree(directory)
        self._temporary_execution_dir = False

    def _publication_record(self, run: BenchmarkRun) -> dict[str, Any]:
        """Describe output recovery using the same measured run and relative artifact identities."""
        return {
            "record": run.record, "exit_code": run.exit_code if run.exit_code is not None else self._exit_code,
            "http_series_published": run.http_series_published,
            "has_measurements": run.measurements is not None,
            "artifacts": {name: Path(os.path.relpath(path, self.execution_dir)).as_posix() for name, path in run.artifacts.items()},
            "output": list(self.benchmark.outputs.destinations),
            "publication_pending": self.benchmark.outputs.includes("wandb") and wandb.setup().settings.mode != "disabled",
        }
