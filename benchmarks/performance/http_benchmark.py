# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run one standard OpenAI-compatible HTTP workload."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from tempfile import TemporaryDirectory
from typing import Any, Optional

from benchmarks.performance.config import HttpBenchmarkConfig
from benchmarks.performance.console_output import log_benchmark_summary
from benchmarks.performance.deployment import BenchmarkRuntimeEndpoint
from benchmarks.performance.evalscope import run_evalscope_standard_load
from benchmarks.performance.local_results import LocalResultDirectory
from benchmarks.performance.request_metrics import (
    attach_user_throughput,
    summarize_request_measurements,
)
from benchmarks.performance.wandb_results import WandbBenchmarkRun

logger = logging.getLogger(__name__)


def resolved_load_record(benchmark: HttpBenchmarkConfig) -> dict[str, Any]:
    """Map internal workload settings to scalar fields used by existing results."""
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
    """Build the run record shared by console and local results."""
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
        record["max_turns"] = benchmark.request_dataset.max_turns
    if benchmark.request_dataset.dataset_selectors == ["random"]:
        record["random_seed"] = benchmark.request_dataset.random_seed
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


def summarize_http_measurements(
    benchmark: HttpBenchmarkConfig,
    request_measurements: dict[str, Any],
    *,
    arrival_rate: float,
    request_count: int,
    reported_concurrency: int,
    include_user_throughput: bool = True,
) -> dict[str, Any]:
    """Aggregate per-request observations and attach the workload coordinates for this run."""
    metrics = summarize_request_measurements(request_measurements)
    configured_stream = bool(benchmark.generation.stream)
    if metrics["stream"] != configured_stream:
        raise RuntimeError(
            "recorded stream mode does not match the requests that ran: "
            f"config={configured_stream} results={metrics['stream']}"
        )
    metrics["rate"] = arrival_rate
    metrics["number"] = request_count
    metrics["parallel"] = reported_concurrency
    if include_user_throughput:
        attach_user_throughput(metrics, parallel=reported_concurrency)
    return metrics


def publish_benchmark_results(
    benchmark: HttpBenchmarkConfig,
    result_directory: LocalResultDirectory,
    run_record: dict[str, Any],
    request_measurements: dict[str, Any],
    metrics: dict[str, Any],
    *,
    wandb_run: Optional[WandbBenchmarkRun] = None,
    trace_measurements: Optional[list[dict[str, Any]]] = None,
    config_snapshot: Optional[dict[str, Any]] = None,
) -> None:
    """Publish console, JSON, and W&B benchmark results according to the selected outputs."""
    if not benchmark.outputs.includes("quiet"):
        log_benchmark_summary(run_record, metrics)
    persisted_config = (
        config_snapshot if config_snapshot is not None else benchmark.to_dict()
    )
    result_directory.save_json(
        "config.json", {**persisted_config, **run_record}
    )
    if "local_artifact" not in request_measurements:
        result_directory.save_json(
            "raw_output.json", request_measurements["results"]
        )
    result_directory.save_json("metrics.json", metrics)
    if wandb_run is not None:
        try:
            if trace_measurements is not None:
                wandb_run.log_trace_measurements(trace_measurements)
            wandb_run.log_metrics(metrics)
        finally:
            wandb_run.finish()
    if result_directory.enabled:
        logger.info("Results saved: %s", result_directory.output_dir)


class StandardHttpLoadBenchmark:
    """Own the request, result, and external-run lifecycle for one standard HTTP workload."""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        endpoint: BenchmarkRuntimeEndpoint,
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
        collect_request_measurements: bool = False,
    ) -> None:
        self.benchmark = benchmark
        self.endpoint = endpoint
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self.collect_request_measurements = collect_request_measurements

    async def run(self) -> dict[str, Any]:
        """Run one standard HTTP workload and return the existing result dictionary."""
        load_record = resolved_load_record(self.benchmark)
        result_directory = open_local_result_directory(
            self.benchmark, self.output_dir
        )
        run_record = build_benchmark_run_record(
            self.benchmark, self.endpoint, "standard_load", load_record
        )
        label = self.label.strip() or None
        working_directory = (
            nullcontext(result_directory.output_dir)
            if result_directory.enabled
            else TemporaryDirectory(prefix="foretoken-benchmark-")
        )

        with working_directory as execution_dir:
            wandb_run = WandbBenchmarkRun()
            wandb_run.start(
                self.benchmark,
                self.endpoint,
                output_dir=execution_dir,
                parallel=int(load_record["resolved_parallel"]),
                rate=float(load_record["rate"]),
                name_suffix=label,
                group=self.wandb_group,
            )
            try:
                metrics, request_measurements = await run_evalscope_standard_load(
                    self.benchmark,
                    self.endpoint,
                    execution_dir,
                    collect_request_measurements=self.collect_request_measurements,
                )
                publish_benchmark_results(
                    self.benchmark,
                    result_directory,
                    run_record,
                    request_measurements,
                    metrics,
                    wandb_run=wandb_run,
                )
            except Exception:
                wandb_run.finish()
                raise

        return {
            "mode": "standard_load",
            "metrics": metrics,
            "raw": request_measurements,
            "output_dir": result_directory.output_dir,
        }
