# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish benchmark runs to the console, a local result directory, and Weights & Biases."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Protocol, Self, cast

import wandb
from foretoken.arguments import ProfileCommand
from foretoken.manifest import DeploymentError
from foretoken.profiling import ProfileRun

from benchmarks.config.benchmark import (
    BenchmarkConfig,
    BenchmarkOutputConfig,
    WandbRunConfig,
)
from benchmarks.model_service import ModelService
from benchmarks.results.console import (
    capture_run_logs,
    log_benchmark_summary,
    print_result_location,
)
from benchmarks.results.environment import (
    OBSERVATION_TIMEOUT_SECONDS,
    ApplicationSources,
    client_environment,
    serving_environment,
)
from benchmarks.results.metrics import RequestMeasurement, summarize_measurements
from benchmarks.results.progress import HttpProgress
from benchmarks.results.prometheus import PrometheusObserver
from benchmarks.results.records import RequestRecordWriter
from benchmarks.results.replicas import KubernetesReplicaObserver
from benchmarks.results.responses import ResponseWriter
from benchmarks.results.wandb import publish_http_wandb
from benchmarks.results.wandb_sync import start_sync

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
    # Execution outcome is distinct from measured scores and aggregate request counts.
    exit_code: int | None = None
    http_series_published: bool = False


class ResultSink(Protocol):
    """One result destination opened before the run executes and closed after publication."""

    def open(self, record: dict[str, Any]) -> None: ...

    def publish(self, run: BenchmarkRun) -> None: ...

    def close(self, *, exit_code: int = 0) -> None: ...


class _ResultConfiguration(Protocol):
    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig

    def to_dict(self) -> dict[str, Any]: ...


_ResultSinkFactory = Callable[[str], list[ResultSink]]
_WandbPublisher = Callable[[Any, BenchmarkRun], None]
_SYSTEM_STATS_INTERVAL_S = 1.0


def wandb_run_timestamp() -> str:
    """Return a local timestamp for W&B run names and generated groups."""
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def wandb_group_name(config: BenchmarkConfig, service: ModelService) -> str:
    """Resolve the explicit group or generate one for a multi-run composition."""
    return config.wandb.group.strip() or f"{service.model}_{wandb_run_timestamp()}"


class ConsoleSink:
    """Print the run summary to the console log."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        log_benchmark_summary(run.record, run.metrics)

    def close(self, *, exit_code: int = 0) -> None:
        return None


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


class BenchmarkArtifactSink:
    """Materialize HTTP configuration and per-request records beside the run's shared metrics."""

    def __init__(self, benchmark: BenchmarkConfig, output_dir: str) -> None:
        self.benchmark = benchmark
        self.output_dir = output_dir
        self.config_path: Path | None = None

    def open(self, record: dict[str, Any]) -> None:
        self.config_path = write_json(
            self.output_dir,
            "config.json",
            {**self.benchmark.to_dict(), **record},
        )

    def publish(self, run: BenchmarkRun) -> None:
        if self.config_path is None:
            raise RuntimeError("benchmark artifact sink is not open")
        run.artifacts["config"] = self.config_path
        if run.measurements is not None and "raw_output" not in run.artifacts:
            slo = run.metrics.get("slo") or {}
            slo_met = slo.get("request_slo_met")
            run.artifacts["raw_output"] = write_json(
                self.output_dir,
                "raw_output.json",
                [
                    {
                        **request_measurement_record(item, stream=bool(run.metrics["stream"])),
                        **(
                            {"slo_met": slo_met[index], "slo_target": slo["request_criteria"]}
                            if isinstance(slo_met, list) else {}
                        ),
                    }
                    for index, item in enumerate(run.measurements)
                ],
                compact=True,
            )

    def close(self, *, exit_code: int = 0) -> None:
        return None


class LocalDirectorySink:
    """Report the persistent HTTP result directory selected by ResultOutputs."""

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        logger.info("Results saved: %s", self.output_dir)

    def close(self, *, exit_code: int = 0) -> None:
        return None


class PlotSink:
    """Export figures from the same saved records used by independent redraws."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Render after artifact sinks finish and expose the figure directory to publishers."""
        from benchmarks.results.plots import render_results

        render_results(self.directory)
        run.artifacts["plots"] = self.directory / "plots"
        logger.info("Plots saved: %s", run.artifacts["plots"])

    def close(self, *, exit_code: int = 0) -> None:
        return None


class WandbSink:
    """Record one native W&B run locally while an independent process synchronizes it."""

    def __init__(
        self,
        benchmark: _ResultConfiguration,
        *,
        execution_dir: str,
        run_name: str,
        group: str,
        publisher: _WandbPublisher,
        run_config: dict[str, Any] | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.execution_dir = execution_dir
        self.run_name = run_name
        self.group = group
        self.publisher = publisher
        self.run_config = run_config
        self._run: Any | None = None
        self._sync: subprocess.Popen[bytes] | None = None
        self._progress_fields: set[tuple[str, str]] = set()

    def open(self, record: dict[str, Any]) -> None:
        """Open native recording before requests, preserving W&B's configured offline/disabled modes."""
        requested_mode = wandb.setup().settings.mode
        wandb_config = self.benchmark.wandb
        run_config = self.run_config or self.benchmark.to_dict()
        init_kwargs: dict[str, Any] = {
            "project": wandb_config.project,
            "name": self.run_name,
            "reinit": "create_new",
            "mode": "disabled" if requested_mode == "disabled" else "offline",
            "config": run_config,
            "dir": self.execution_dir,
            "settings": wandb.Settings(
                silent=True,
                x_stats_sampling_interval=_SYSTEM_STATS_INTERVAL_S,
            ),
        }
        if self.group:
            init_kwargs["group"] = self.group
        if wandb_config.entity:
            init_kwargs["entity"] = wandb_config.entity
        try:
            self._run = wandb.init(**init_kwargs)
        except wandb.errors.Error:
            logger.exception("W&B local recording initialization failed")
            raise
        if self._run is None:
            raise RuntimeError("W&B initialization returned no run")
        if requested_mode != "disabled":
            directory = Path(self._run.settings.sync_dir).resolve()
            print_result_location(f"W&B local run: {directory}")
            if requested_mode not in {"offline", "dryrun"}:
                try:
                    self._sync = start_sync(directory)
                except OSError as error:
                    logger.error("W&B live sync could not start: %s; local run retained at %s", error, directory)

    def log_progress(self, row: dict[str, Any], *, axis: str) -> None:
        """Bind live fields to their domain axis and queue them through the owned run."""
        if (axis, axis) not in self._progress_fields:
            self._run.define_metric(axis, hidden=True)
            self._progress_fields.add((axis, axis))
        for key in row.keys() - {axis}:
            if (key, axis) not in self._progress_fields:
                self._run.define_metric(key, step_metric=axis, step_sync=False)
                self._progress_fields.add((key, axis))
        self._run.log(row)

    def publish(self, run: BenchmarkRun) -> None:
        """Publish benchmark content through the adapter selected by ResultOutputs."""
        if self._run is None:
            raise RuntimeError("W&B sink is not open")
        try:
            self.publisher(self._run, run)
            if "warmup_metrics" in run.artifacts:
                artifact = wandb.Artifact(f"warmup-{self._run.id}", type="benchmark-warmup")
                for name in ("warmup_metrics", "warmup_raw_output"):
                    artifact.add_file(str(run.artifacts[name]), name=run.artifacts[name].name)
                self._run.log_artifact(artifact)
            if "plots" in run.artifacts:
                directory = run.artifacts["plots"]
                artifact = wandb.Artifact(f"plots-{self._run.id}", type="benchmark-plots")
                artifact.add_dir(str(directory))
                self._run.log_artifact(artifact)
        except wandb.errors.Error:
            logger.exception("W&B local recording failed")
            raise

    def close(self, *, exit_code: int = 0) -> None:
        """Seal local records with the benchmark status and release sync without waiting for upload."""
        run = self._run
        self._run = None
        if run is None:
            return
        try:
            try:
                journals: dict[str, list[Path]] = {}
                for path in sorted(Path(self.execution_dir).glob("*.jsonl")):
                    kind = "responses" if path.name in {"responses.jsonl", "warmup_responses.jsonl"} else "records"
                    journals.setdefault(kind, []).append(path)
                for kind, paths in journals.items():
                    artifact = wandb.Artifact(f"{kind}-{run.id}", type=f"benchmark-{kind}")
                    for path in paths:
                        artifact.add_file(str(path), name=path.name)
                    run.log_artifact(artifact)
                log_path = Path(self.execution_dir) / "run.log"
                if log_path.is_file():
                    artifact = wandb.Artifact(f"logs-{run.id}", type="benchmark-logs")
                    artifact.add_file(str(log_path), name="run.log")
                    run.log_artifact(artifact)
            finally:
                run.finish(exit_code=exit_code)
        except wandb.errors.Error:
            logger.exception("W&B local recording finalization failed")
            raise
        finally:
            if self._sync is not None:
                self._sync.stdin.close()
                self._sync = None


def result_directory_path(
    benchmark: _ResultConfiguration,
    output_dir: str | None = None,
    directory_prefix: str = "",
) -> str:
    """Return an explicit child path, or reserve a unique local directory under ``--output-dir``."""
    if output_dir is not None:
        return output_dir
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    if benchmark.outputs.saves_local:
        os.makedirs(benchmark.outputs.output_dir, exist_ok=True)
        return mkdtemp(
            prefix=f"{directory_prefix}{timestamp}-",
            dir=benchmark.outputs.output_dir,
        )
    return os.path.join(
        benchmark.outputs.output_dir,
        f"{directory_prefix}{timestamp}",
    )


def write_json(directory: str, filename: str, data: Any, *, compact: bool = False) -> Path:
    """Atomically replace a JSON artifact so interrupted writes leave the previous record readable."""
    path = Path(directory) / filename
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        if compact:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, file, indent=4, ensure_ascii=False)
    temporary.replace(path)
    return path


def write_gpu_allocation_csv(directory: str, allocation: dict[str, Any]) -> Path:
    """Write every GPU snapshot and failed-read boundary for local inspection."""
    resources = sorted({
        resource
        for sample in allocation["samples"]
        for resource in sample["gpu_counts"]
    })
    rows = [
        {
            "elapsed_time_s": sample["elapsed_time_s"],
            "event": "sample",
            **{resource: sample["gpu_counts"].get(resource) for resource in resources},
        }
        for sample in allocation["samples"]
    ]
    rows.extend(
        {
            "elapsed_time_s": elapsed,
            "event": "failed_read",
            **dict.fromkeys(resources),
        }
        for elapsed in allocation["failed_read_elapsed_s"]
    )
    rows.sort(key=lambda row: (float(row["elapsed_time_s"]), row["event"]))
    path = Path(directory) / "gpu_allocation.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("elapsed_time_s", "event", *resources))
        writer.writeheader()
        writer.writerows(rows)
    return path


def resolved_load_record(benchmark: BenchmarkConfig) -> dict[str, Any]:
    """Map internal workload settings to scalar fields used by result consumers."""
    load = benchmark.load
    max_concurrency = int(load.max_concurrency)
    return {
        "max_concurrency": max_concurrency,
        "num_prompts": load.request_count,
        "request_rate": float(load.arrival_rate),
        "duration": load.duration_seconds,
        "open_loop": max_concurrency == -1,
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
        "model": service.model or "per dataset row",
        "url": service.chat_completions_url,
        "max_concurrency": load_record["max_concurrency"],
        "num_prompts": load_record["num_prompts"],
        "request_rate": load_record["request_rate"],
        "duration": load_record.get("duration"),
        "open_loop": load_record["open_loop"],
        "stream": benchmark.generation.stream,
        "resolved": {
            "max_concurrency": load_record["max_concurrency"],
            "num_prompts": load_record["num_prompts"],
            "request_rate": load_record["request_rate"],
        },
    }
    if benchmark.is_multi_turn:
        record["multi_turn"] = True
        record["max_turns"] = workload.max_turns
        record["conversation_history"] = workload.conversation_history
    if workload.dataset_selectors == ["random"]:
        record["random_seed"] = workload.random_seed
    return record


class ResultOutputs:
    """Own preparation logs, measurement observers, publication, and cleanup for one run.

    File outputs and W&B synchronization retain the execution directory.
    Console-only execution uses a temporary directory, removed after success
    and retained when local recording or benchmark execution fails.
    """

    def __init__(
        self,
        benchmark: _ResultConfiguration,
        service: ModelService | None,
        *,
        label: str = "",
        output_dir: str | None = None,
        wandb_group: str | None = None,
        directory_prefix: str = "",
        sink_factory: _ResultSinkFactory | None = None,
    ) -> None:
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

    @property
    def execution_dir(self) -> str:
        """Return the local directory used by the benchmark engine for this run."""
        if self._execution_dir is None:
            raise RuntimeError("result outputs are not active")
        return self._execution_dir

    def create_response_writer(self) -> ResponseWriter | None:
        """Open optional content evidence under this run and close it with the result lifecycle."""
        if not self.benchmark.outputs.includes("raw"):
            return None
        writer = ResponseWriter(self.execution_dir)
        self._resources.callback(writer.close)
        return writer

    def create_profile(self) -> Any | None:
        """Create the optional HTTP capture observer owned by this point's output directory."""
        if self._execution_dir is None:
            raise RuntimeError("result outputs are not active")
        if not isinstance(self.benchmark, BenchmarkConfig):
            raise TypeError("profiling is only supported for HTTP benchmark outputs")
        profile_options = self.benchmark.profile
        if profile_options is None:
            return None
        # Keep the capture adapter's import local to avoid a results/capture cycle.
        from benchmarks.profiling.capture import BenchmarkProfile

        command = ProfileCommand(
            kustomize_path=self.benchmark.service.kustomize_path,
            model=self.service.model if self.service is not None else self.benchmark.service.model,
            profile_engine=profile_options.engine,
            profile_duration=profile_options.duration,
            timeout=self.benchmark.service.wait_timeout,
        )
        return BenchmarkProfile(
            ProfileRun(command, deployment=self.service.deployment if self.service is not None else None),
            self.execution_dir,
        )

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
            self._context = {
                "status": "running", "phase": "preparation", "exit_code": None,
                "measurement_exit_code": None,
                "started_at": datetime.now(UTC).isoformat(), "finished_at": None,
            }
            write_json(self.execution_dir, "context.json", self._context)
            self._resources.enter_context(
                capture_run_logs(self.execution_dir, quiet=outputs.includes("quiet"))
            )
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def start_measurement(self) -> None:
        """Record actual execution or checkpoint reuse before the leaf runner begins measurement."""
        if self._context["phase"] == "preparation":
            self._context["phase"] = "measurement"
            write_json(self.execution_dir, "context.json", self._context)

    def finish_measurement(self, exit_code: int) -> None:
        """Retain the leaf's confirmed outcome before progress, transport, or profile cleanup."""
        self._context.update(phase="finalization", measurement_exit_code=exit_code)
        self._exit_code = exit_code
        write_json(self.execution_dir, "context.json", self._context)

    def save_measurement(self, run: BenchmarkRun) -> None:
        """Retain actual leaf metrics and its explicit exit code before resource finalization."""
        self.finish_measurement(cast(int, run.exit_code))
        run.artifacts["metrics"] = write_json(self.execution_dir, "metrics.json", run.metrics)

    def open(self, record: dict[str, Any], *, start_observers: bool = True) -> None:
        """Open result sinks and configuration; callers may defer observation until after warmup."""
        directory = self.execution_dir
        outputs = self.benchmark.outputs
        sinks: list[ResultSink] = []
        if self.sink_factory is not None:
            sinks = self.sink_factory(directory)
        else:
            if not isinstance(self.benchmark, BenchmarkConfig):
                raise TypeError(
                    "non-standard benchmark results require a sink factory"
                )
            if self.service is None:
                raise TypeError(
                    "standard benchmark results require a service"
                )
            standard_benchmark = cast(BenchmarkConfig, self.benchmark)
            if outputs.saves_local:
                sinks.append(
                    BenchmarkArtifactSink(
                        standard_benchmark,
                        directory,
                    )
                )
            sinks.append(ConsoleSink())
            if outputs.saves_local:
                sinks.append(
                    LocalDirectorySink(directory)
                )
            if outputs.includes("wandb"):
                base_name = (
                    standard_benchmark.wandb.run_name.strip()
                    or f"{self.service.model or 'dataset-models'}_{wandb_run_timestamp()}"
                )
                run_name = (
                    f"{base_name}_{self.label}" if self.label else base_name
                )
                group = (
                    standard_benchmark.wandb.group.strip()
                    or self.wandb_group
                    or ""
                )
                run_config = standard_benchmark.to_dict()
                run_config["service"]["model"] = self.service.model
                run_config["model"] = self.service.model
                if self.service.model_service_refs:
                    run_config["declared_gpu_count"] = self.service.gpu_count
                sinks.append(
                    WandbSink(
                        standard_benchmark,
                        execution_dir=directory,
                        run_name=run_name,
                        group=group,
                        publisher=publish_http_wandb,
                        run_config=run_config,
                    )
                )
        if outputs.includes("plot"):
            index = next((index for index, sink in enumerate(sinks) if isinstance(sink, WandbSink)), len(sinks))
            sinks.insert(index, PlotSink(directory))
        for sink in sinks:
            self._resources.callback(self._close_sink, sink)
            sink.open(record)
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        if outputs.saves_local:
            self._environment = {
                "client": client_environment(deadline=deadline),
            }
            write_json(self.execution_dir, "environment.json", self._environment)
        self._sinks = sinks
        if start_observers:
            self.start_observers(deadline=deadline)

    def publish_progress(self, row: dict[str, Any], *, axis: str) -> None:
        """Send a measured progress snapshot to each selected live destination."""
        for sink in self._sinks:
            if isinstance(sink, WandbSink):
                sink.log_progress(row, axis=axis)

    def create_request_writer(self, name: str) -> RequestRecordWriter:
        """Open a completed-request journal and close it with this run's result resources."""
        writer = RequestRecordWriter(Path(self.execution_dir) / f"{name}.jsonl")
        self._resources.callback(writer.close)
        return writer

    def http_progress(self, *, stream: bool, criteria: dict[str, str] | None = None) -> HttpProgress | None:
        """Connect the HTTP runner to the selected live W&B destination."""
        if self.benchmark.outputs.includes("wandb"):
            return HttpProgress(self.publish_progress, stream=stream, criteria=criteria)
        return None

    def capture_source_evidence(self, snapshot: dict[str, Any], *, deadline: float | None = None) -> None:
        """Retain provenance and local archives from an already-observed service at a run boundary."""
        if not self.benchmark.outputs.saves_local:
            return
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        self._applications.capture(snapshot, deadline=deadline)
        self._applications.save_archives(deadline=deadline)

    def start_observers(self, *, deadline: float | None = None) -> None:
        """Prepare observations within one budget; deferred starts get a fresh budget after warmup."""
        outputs = self.benchmark.outputs
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        if self._environment is not None and self.service is not None:
            self._environment["before"] = serving_environment(self.service, deadline=deadline)
            self.capture_source_evidence(self._environment["before"], deadline=deadline)
            write_json(self.execution_dir, "environment.json", self._environment)
        if (
            self.service is not None
            and self.service.model_service_refs
            and outputs.saves_local
        ):
            try:
                observer = KubernetesReplicaObserver(
                    self.service.model_service_refs,
                    self.service.model,
                    self._applications,
                )
                observer.start(deadline=deadline)
            except (DeploymentError, RuntimeError) as exc:
                logger.warning(
                    "Replica observation unavailable; continuing with benchmark: %s",
                    exc,
                )
            else:
                self._replica_observer = observer
                self._resources.callback(self._close_replica_observer)
            if self._replica_observer is None:
                return
            try:
                prometheus_observer = PrometheusObserver(self.service, self._replica_observer)
                prometheus_observer.start(deadline=deadline)
            except (DeploymentError, RuntimeError) as exc:
                logger.warning(
                    "Prometheus observation unavailable; continuing with benchmark: %s",
                    exc,
                )
            else:
                self._prometheus_observer = prometheus_observer
                self._resources.callback(self._close_prometheus_observer)

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
            logger.error("Benchmark artifacts preserved: %s", directory)
        elif self._temporary_execution_dir:
            shutil.rmtree(directory)
        self._temporary_execution_dir = False

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
            logger.error("Benchmark stopped: %s", error)
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
                write_json(self.execution_dir, "context.json", self._context)
            except OSError:
                if error is None:
                    self._exit_code = 1
                    raise
                logger.exception("Could not retain final run context")
            finally:
                self._release_execution_directory(preserve=self._exit_code != 0)
                self._sinks = []
                self._execution_dir = None
                self._replica_observer = None
                self._prometheus_observer = None
                self._exit_code = 0
        return False

    def record_warmup(self, rows: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
        """Persist the completed warmup before measurement or a warmup failure."""
        write_json(self.execution_dir, "warmup_raw_output.json", rows, compact=True)
        write_json(self.execution_dir, "warmup_metrics.json", metrics)

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
            include_normalized_throughput=False,
            itl_summary=itl_summary,
        )
        self.record_warmup(
            [request_measurement_record(item, stream=stream) for item in measurements], metrics,
        )

    def publish(self, run: BenchmarkRun) -> None:
        """Save completed measurements before bounded observation finalization and presentation."""
        self._context["phase"] = "finalization"
        write_json(self.execution_dir, "context.json", self._context)
        run.artifacts["metrics"] = write_json(self.execution_dir, "metrics.json", run.metrics)
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
        if run.exit_code is not None:
            self._exit_code = run.exit_code
        elif int(run.metrics["success_num"]) == 0:
            self._exit_code = 1
        if self.benchmark.outputs.includes("quiet") and run.measurements is not None and run.metrics["failed_num"]:
            logger.error(
                "%s/%s requests failed; see %s",
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
                        self.execution_dir,
                        "replica_observations.json",
                        observations,
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
                self.execution_dir,
                "prometheus_observations.json",
                observations,
            )
        if self._environment is not None:
            if self.service is not None:
                self._environment["after"] = serving_environment(
                    self.service, deadline=deadline,
                )
            if "after" in self._environment:
                self._applications.capture(self._environment["after"], deadline=deadline)
            self._applications.save_archives(deadline=deadline)
            if self._applications.records:
                self._environment["applications"] = self._applications.records
            run.artifacts["environment"] = write_json(
                self.execution_dir, "environment.json", self._environment,
            )
        write_json(self.execution_dir, "metrics.json", run.metrics)
        for sink in self._sinks:
            sink.publish(run)
