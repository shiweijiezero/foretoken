# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish benchmark runs to the console, a local result directory, and Weights & Biases."""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable, Optional, Protocol, cast

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
from benchmarks.results.console import capture_run_logs, log_benchmark_summary
from benchmarks.results.environment import client_environment, serving_environment
from benchmarks.results.metrics import RequestMeasurement
from benchmarks.results.prometheus import PrometheusObserver
from benchmarks.results.replicas import KubernetesReplicaObserver
from benchmarks.results.wandb import publish_http_wandb

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
    # Quality evaluations have an execution status independent of the model's score.
    exit_code: int | None = None


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
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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


class BenchmarkArtifactSink:
    """Materialize HTTP configuration, metrics, and per-request records in the execution directory."""

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
        run.artifacts["metrics"] = write_json(
            self.output_dir,
            "metrics.json",
            run.metrics,
        )
        if run.measurements is not None and "raw_output" not in run.artifacts:
            slo = run.metrics.get("slo") or {}
            slo_met = slo.get("request_slo_met")
            write_json(
                self.output_dir,
                "raw_output.json",
                [
                    {
                        "success": item.succeeded,
                        **(
                            {"slo_met": slo_met[index]}
                            if isinstance(slo_met, list)
                            else {}
                        ),
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
                        "cached_input_tokens": item.cached_input_tokens,
                        "inter_token_latencies": list(item.itl_samples)
                        if run.metrics["stream"]
                        else [],
                        "conversation_id": item.conversation_id,
                        "turn": item.turn,
                        "dataset": item.dataset,
                    }
                    for index, item in enumerate(run.measurements)
                ],
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


class WandbSink:
    """Own one W&B SDK run and delegate benchmark-specific content publication."""

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

    def open(self, record: dict[str, Any]) -> None:
        """Create the SDK run before requests so system metrics cover execution."""
        wandb_config = self.benchmark.wandb
        run_config = self.run_config or self.benchmark.to_dict()
        init_kwargs: dict[str, Any] = {
            "project": wandb_config.project,
            "name": self.run_name,
            "reinit": "create_new",
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
            logger.exception("W&B initialization failed")
            raise
        if self._run is None:
            raise RuntimeError("W&B initialization returned no run")
        logger.info(
            "W&B logging enabled: project=%s name=%s group=%s",
            wandb_config.project,
            self.run_name,
            self.group or "-",
        )

    def publish(self, run: BenchmarkRun) -> None:
        """Publish benchmark content through the adapter selected by ResultOutputs."""
        if self._run is None:
            raise RuntimeError("W&B sink is not open")
        try:
            self.publisher(self._run, run)
        except wandb.errors.Error:
            logger.exception("W&B publication failed")
            raise

    def close(self, *, exit_code: int = 0) -> None:
        """Finish the owned SDK run once with the benchmark exit status."""
        run = self._run
        self._run = None
        if run is None:
            return
        try:
            run.finish(exit_code=exit_code)
        except wandb.errors.Error:
            logger.exception("W&B finalization failed")
            raise


def result_directory_path(
    benchmark: _ResultConfiguration,
    output_dir: Optional[str] = None,
    directory_prefix: str = "",
) -> str:
    """Return an explicit child path, or reserve a unique local directory under ``--output-dir``."""
    if output_dir is not None:
        return output_dir
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if benchmark.outputs.includes("local"):
        os.makedirs(benchmark.outputs.output_dir, exist_ok=True)
        return mkdtemp(
            prefix=f"{directory_prefix}{timestamp}-",
            dir=benchmark.outputs.output_dir,
        )
    return os.path.join(
        benchmark.outputs.output_dir,
        f"{directory_prefix}{timestamp}",
    )


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
        "model": service.model,
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

    The execution directory is the local result directory when local output is
    enabled and a temporary directory otherwise; the engine writes its files
    there. Temporary files are removed after success and retained when an
    explicitly selected W&B lifecycle or benchmark execution fails.
    """

    def __init__(
        self,
        benchmark: _ResultConfiguration,
        service: ModelService | None,
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
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
        self._exit_code = 0

    @property
    def execution_dir(self) -> str:
        """Return the local directory used by the benchmark engine for this run."""
        if self._execution_dir is None:
            raise RuntimeError("result outputs are not active")
        return self._execution_dir

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

    def __enter__(self) -> ResultOutputs:
        """Acquire the execution directory and capture preparation through publication."""
        if self._execution_dir is not None:
            raise RuntimeError("result outputs are already active")
        outputs = self.benchmark.outputs
        if outputs.includes("local"):
            self._execution_dir = result_directory_path(
                self.benchmark,
                self.output_dir,
                self.directory_prefix,
            )
        else:
            self._temporary_execution_dir = True
            if outputs.includes("wandb"):
                os.makedirs(outputs.output_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self._execution_dir = mkdtemp(
                    prefix=f"{self.directory_prefix}{timestamp}-",
                    dir=outputs.output_dir,
                )
            else:
                self._execution_dir = mkdtemp(
                    prefix="foretoken-benchmark-"
                )

        try:
            os.makedirs(self.execution_dir, exist_ok=True)
            self._resources.enter_context(
                capture_run_logs(self.execution_dir, quiet=outputs.includes("quiet"))
            )
        except BaseException:
            self._release_execution_directory(preserve=outputs.includes("wandb"))
            self._execution_dir = None
            raise
        return self

    def open(self, record: dict[str, Any]) -> None:
        """Start publication and measurement observers after workload preparation or warmup."""
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
            if outputs.includes("local") or outputs.includes("wandb"):
                sinks.append(
                    BenchmarkArtifactSink(
                        standard_benchmark,
                        directory,
                    )
                )
            if not outputs.includes("quiet"):
                sinks.append(ConsoleSink())
            if outputs.includes("local"):
                sinks.append(
                    LocalDirectorySink(directory)
                )
            if outputs.includes("wandb"):
                base_name = (
                    standard_benchmark.wandb.run_name.strip()
                    or f"{self.service.model}_{wandb_run_timestamp()}"
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
        for sink in sinks:
            self._resources.callback(self._close_sink, sink)
            sink.open(record)
        if outputs.includes("local") or outputs.includes("wandb"):
            self._environment = {
                "client": client_environment(),
            }
            if self.service is not None:
                self._environment["before"] = serving_environment(
                    self.service
                )
            write_json(self.execution_dir, "environment.json", self._environment)
        if (
            self.service is not None
            and self.service.model_service_refs
            and (
                outputs.includes("local")
                or outputs.includes("wandb")
            )
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
            try:
                prometheus_observer = PrometheusObserver(self.service)
                prometheus_observer.start()
            except (DeploymentError, RuntimeError) as exc:
                logger.warning(
                    "Prometheus observation unavailable; continuing with benchmark: %s",
                    exc,
                )
            else:
                self._prometheus_observer = prometheus_observer
                self._resources.callback(self._close_prometheus_observer)
        self._sinks = sinks

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
        failed = exc_type is not None or self._exit_code != 0
        self._exit_code = 1 if failed else 0
        try:
            try:
                return self._resources.__exit__(
                    exc_type, exc_value, traceback
                )
            except BaseException:
                failed = True
                if exc_type is not None:
                    logger.exception("Result cleanup also failed")
                    return False
                raise
        finally:
            self._release_execution_directory(
                preserve=failed
            )
            self._sinks = []
            self._execution_dir = None
            self._replica_observer = None
            self._prometheus_observer = None
            self._exit_code = 0

    def publish(self, run: BenchmarkRun) -> None:
        """Stop observations, record the run status, and publish every open sink."""
        if run.exit_code is not None:
            self._exit_code = run.exit_code
        elif int(run.metrics["success_num"]) == 0:
            self._exit_code = 1
        if self.benchmark.outputs.includes("quiet"):
            run.artifacts["console_log"] = Path(self.execution_dir) / "run.log"
            if run.measurements is not None and run.metrics["failed_num"]:
                logger.error(
                    "%s/%s requests failed; see %s",
                    run.metrics["failed_num"], run.metrics["request_num"], self.execution_dir,
                )
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
        prometheus_observer = self._prometheus_observer
        self._prometheus_observer = None
        if prometheus_observer is not None:
            observations = prometheus_observer.finish(run.time_origin)
            run.artifacts["prometheus_observations"] = write_json(
                self.execution_dir,
                "prometheus_observations.json",
                observations,
            )
        if self._environment is not None:
            if self.service is not None:
                self._environment["after"] = serving_environment(
                    self.service
                )
            run.artifacts["environment"] = write_json(
                self.execution_dir, "environment.json", self._environment,
            )
        for sink in self._sinks:
            sink.publish(run)
