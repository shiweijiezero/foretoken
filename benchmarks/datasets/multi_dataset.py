# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run multiple chat request datasets in order and merge the HTTP benchmark results."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Any, Callable

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.metrics import RequestMeasurement, summarize_measurements
from benchmarks.results.output import (
    BenchmarkRun,
    ConsoleSink,
    LocalDirectorySink,
    ResultSink,
    build_benchmark_run_record,
    resolved_load_record,
    result_directory_path,
)
from benchmarks.results.wandb import wandb_group_name

logger = logging.getLogger(__name__)


def _dataset_directory_name(index: int, dataset_selector: str) -> str:
    safe_name = re.sub(r"[^\w.\-]+", "_", dataset_selector).strip("_")
    return f"{index:02d}_{safe_name or 'dataset'}"


def _allocate_request_counts(
    request_count: int,
    dataset_count: int,
) -> list[int]:
    """Distribute the total request count as evenly as possible in dataset order."""
    base_count, remainder = divmod(request_count, dataset_count)
    return [
        base_count + (1 if index < remainder else 0)
        for index in range(dataset_count)
    ]


class MultiDatasetBenchmark:
    """Own multi-dataset request allocation, child workload order, and merged results."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        run_dataset: Callable[[BenchmarkConfig, ModelService, str, str, str | None], BenchmarkRun],
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self._run_dataset = run_dataset

    def run(self) -> BenchmarkRun:
        """Benchmark each dataset in order and publish one merged result."""
        load_record = resolved_load_record(self.benchmark)
        dataset_selectors = list(
            self.benchmark.resolved_workload.dataset_selectors
        )
        total_requests = int(load_record["number"])
        request_counts = _allocate_request_counts(
            total_requests, len(dataset_selectors)
        )
        record = build_benchmark_run_record(
            self.benchmark,
            self.service,
            "multi_dataset",
            load_record,
        )
        record["datasets"] = dataset_selectors
        record["dataset_request_counts"] = request_counts

        # The merged result is printed and saved locally; each child dataset
        # owns its own W&B run inside the shared group.
        output_dir = result_directory_path(self.benchmark)
        sinks: list[ResultSink] = []
        if not self.benchmark.outputs.includes("quiet"):
            sinks.append(ConsoleSink())
        if self.benchmark.outputs.includes("local"):
            sinks.append(LocalDirectorySink(self.benchmark, output_dir))
        for sink in sinks:
            sink.open(record)

        wandb_group = (
            wandb_group_name(self.benchmark, self.service)
            if self.benchmark.outputs.includes("wandb")
            else None
        )

        measurements: list[RequestMeasurement] = []
        total_time = 0.0
        dataset_results: list[dict[str, Any]] = []
        for index, (dataset_selector, request_count) in enumerate(
            zip(dataset_selectors, request_counts)
        ):
            if request_count == 0:
                logger.info(
                    "Skipping dataset %s (allocated 0 of total %s)",
                    dataset_selector,
                    total_requests,
                )
                continue

            logger.info(
                "Dataset %s/%s: %s (number=%s)",
                index + 1,
                len(dataset_selectors),
                dataset_selector,
                request_count,
            )
            child_name = _dataset_directory_name(index, dataset_selector)
            child_benchmark = replace(
                self.benchmark,
                workload=replace(
                    self.benchmark.resolved_workload,
                    dataset_selectors=[dataset_selector],
                ),
                load=replace(
                    self.benchmark.load,
                    request_count=request_count,
                ),
            )
            child = self._run_dataset(
                child_benchmark,
                self.service,
                child_name,
                os.path.join(output_dir, child_name),
                wandb_group,
            )
            if child.measurements is None:
                raise RuntimeError("generated load did not return measurements")
            measurements.extend(child.measurements)
            total_time += float(child.metrics["benchmark_time"])
            dataset_results.append(
                {"dataset": dataset_selector, "metrics": child.metrics}
            )

        if not dataset_results:
            raise ValueError(
                f"No requests dispatched for datasets={dataset_selectors} "
                f"with total number={total_requests}"
            )

        metrics = summarize_measurements(
            measurements,
            total_time=total_time,
            stream=self.benchmark.generation.stream,
            arrival_rate=load_record["rate"],
            request_count=total_requests,
            reported_concurrency=load_record["resolved_parallel"],
        )
        if self.benchmark.is_multi_turn:
            empty_distribution = {
                "mean": None,
                "p50": None,
                "p95": None,
                "p99": None,
            }
            child_conversations = [
                {
                    "dataset": child["dataset"],
                    **child["metrics"]["conversation"],
                }
                for child in dataset_results
            ]
            successful_turns = sum(
                int(child["metrics"]["success_num"])
                for child in dataset_results
            )
            weighted_context_turns = sum(
                float(
                    child["metrics"]["conversation"][
                        "avg_context_turns_per_request"
                    ]
                    or 0.0
                )
                * int(child["metrics"]["success_num"])
                for child in dataset_results
            )
            metrics["multi_turn"] = True
            metrics["throughput"]["attempted_conversations_per_second"] = (
                total_requests / metrics["benchmark_time"]
                if metrics["benchmark_time"] > 0
                else 0.0
            )
            metrics["conversation"] = {
                "attempted_num": total_requests,
                "max_turns": self.benchmark.resolved_workload.max_turns,
                "avg_turn_requests": (
                    metrics["request_num"] / total_requests
                    if total_requests
                    else 0.0
                ),
                "avg_context_turns_per_request": (
                    weighted_context_turns / successful_turns
                    if successful_turns
                    else None
                ),
                # EvalScope 1.11.1 persists HTTP turns without trace IDs. Keep
                # exact conversation distributions in each child result instead
                # of averaging dataset percentiles into a false global value.
                "latency": dict(empty_distribution),
                "first_turn_ttft": dict(empty_distribution),
                "time_to_final_answer_token": dict(empty_distribution),
                "decode_tokens_per_second": dict(empty_distribution),
                "cache_hit_rate_percent": dict(empty_distribution),
                "eligible_cache_hit_rate_percent": dict(empty_distribution),
                "per_dataset": child_conversations,
            }
        run = BenchmarkRun(
            record=record,
            metrics=metrics,
            measurements=measurements,
            artifacts={},
        )
        for sink in sinks:
            sink.publish(run)
        for sink in sinks:
            sink.close()
        return run
