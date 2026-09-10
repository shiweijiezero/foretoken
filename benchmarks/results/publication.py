# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own benchmark result directories and publication lifecycles."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from tempfile import TemporaryDirectory
from typing import Any, Optional

from benchmarks.config import HttpBenchmarkConfig
from benchmarks.results.console import log_benchmark_summary
from benchmarks.deployment import BenchmarkRuntimeEndpoint
from benchmarks.results.wandb import WandbBenchmarkRun

logger = logging.getLogger(__name__)


class LocalResultDirectory:
    """Own the local JSON artifact directory for one workload point or experiment."""

    def __init__(
        self,
        root_dir: str | None = None,
        *,
        output_dir: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        if output_dir is None:
            if root_dir is None:
                raise ValueError("root_dir is required when output_dir is omitted")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(root_dir, timestamp)
        self.output_dir = output_dir
        self.enabled = enabled
        if enabled:
            os.makedirs(self.output_dir, exist_ok=True)

    def save_json(self, filename: str, data: Any) -> Optional[str]:
        """Write one JSON artifact and return its path when local output is enabled."""
        if not self.enabled:
            return None
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        return path


def resolved_load_record(benchmark: HttpBenchmarkConfig) -> dict[str, Any]:
    """Map internal workload settings to scalar fields used by result consumers."""
    schedule = benchmark.load_schedule
    max_concurrency = int(schedule.max_concurrency)
    return {
        "parallel": max_concurrency,
        "number": int(schedule.request_count),
        "rate": float(schedule.arrival_rate),
        "open_loop": schedule.unbounded_concurrency,
        "resolved_parallel": (
            -1 if schedule.unbounded_concurrency else max_concurrency
        ),
    }


def build_benchmark_run_record(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
    mode: str,
    load_record: dict[str, Any],
) -> dict[str, Any]:
    """Build the run record shared by console output and local artifacts."""
    record = {
        "mode": mode,
        "model": endpoint.model,
        "url": endpoint.url,
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
        record["max_turns"] = benchmark.resolved_dataset.max_turns
    if benchmark.resolved_dataset.dataset_selectors == ["random"]:
        record["random_seed"] = benchmark.resolved_dataset.random_seed
    return record


def open_local_result_directory(
    benchmark: HttpBenchmarkConfig,
    output_dir: Optional[str] = None,
) -> LocalResultDirectory:
    """Resolve the unique local result directory for one run or experiment root."""
    enabled = benchmark.outputs.includes("local")
    if output_dir is not None:
        return LocalResultDirectory(output_dir=output_dir, enabled=enabled)
    return LocalResultDirectory(
        root_dir=benchmark.outputs.output_dir,
        enabled=enabled,
    )


def publish_results(
    benchmark: HttpBenchmarkConfig,
    result_directory: LocalResultDirectory,
    run_record: dict[str, Any],
    request_measurements: dict[str, Any],
    metrics: dict[str, Any],
    *,
    wandb_run: WandbBenchmarkRun | None = None,
    trace_measurements: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Publish console and local artifacts, and optionally log an active W&B run."""
    if not benchmark.outputs.includes("quiet"):
        log_benchmark_summary(run_record, metrics)
    result_directory.save_json(
        "config.json", {**benchmark.to_dict(), **run_record}
    )
    if "local_artifact" not in request_measurements:
        result_directory.save_json(
            "raw_output.json", request_measurements["results"]
        )
    result_directory.save_json("metrics.json", metrics)
    if wandb_run is not None:
        if trace_measurements is not None:
            wandb_run.log_trace_measurements(trace_measurements)
        wandb_run.log_metrics(metrics)
    if result_directory.enabled:
        logger.info("Results saved: %s", result_directory.output_dir)


class ResultPublication:
    """Own local output, temporary execution files, W&B, and final publication for one run."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
        run_record: dict[str, Any],
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint
        self.run_record = run_record
        self.result_directory = open_local_result_directory(benchmark, output_dir)
        self.label = label.strip() or None
        self.wandb_group = wandb_group
        self.wandb_run = WandbBenchmarkRun()
        self._temporary_directory: TemporaryDirectory[str] | None = None
        self._execution_dir: str | None = None

    @property
    def output_dir(self) -> str:
        """Return the configured result path, whether or not local output is enabled."""
        return self.result_directory.output_dir

    @property
    def execution_dir(self) -> str:
        """Return the local directory used by the benchmark engine for this run."""
        if self._execution_dir is None:
            raise RuntimeError("result publication is not active")
        return self._execution_dir

    def __enter__(self) -> ResultPublication:
        """Acquire the execution directory and optional W&B run for one workload."""
        if self._execution_dir is not None:
            raise RuntimeError("result publication is already active")
        if self.result_directory.enabled:
            self._execution_dir = self.result_directory.output_dir
        else:
            self._temporary_directory = TemporaryDirectory(
                prefix="foretoken-benchmark-"
            )
            self._execution_dir = self._temporary_directory.name
        try:
            resolved = self.run_record["resolved"]
            self.wandb_run.start(
                self.benchmark,
                self.endpoint,
                output_dir=self.execution_dir,
                parallel=int(resolved["parallel"]),
                rate=float(resolved["rate"]),
                name_suffix=self.label,
                group=self.wandb_group,
            )
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        """Finish W&B once and remove temporary execution files after success or failure."""
        try:
            try:
                self.wandb_run.finish()
            except Exception:
                if exc_type is None:
                    raise
                logger.exception("Failed to finish W&B benchmark run")
        finally:
            self._cleanup()
        return False

    def _cleanup(self) -> None:
        self._execution_dir = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def publish(
        self,
        request_measurements: dict[str, Any],
        metrics: dict[str, Any],
        *,
        trace_measurements: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Publish console, JSON, and W&B results before the owner closes resources."""
        publish_results(
            self.benchmark,
            self.result_directory,
            self.run_record,
            request_measurements,
            metrics,
            wandb_run=self.wandb_run,
            trace_measurements=trace_measurements,
        )
