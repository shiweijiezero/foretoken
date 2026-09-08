# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""依次执行多个聊天请求数据集并合并 HTTP 性能结果。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Any

from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.http_benchmark import (
    StandardHttpLoadBenchmark,
    build_benchmark_run_record,
    open_local_result_directory,
    publish_benchmark_results,
    resolved_load_record,
    summarize_http_measurements,
)
from benchmarks.performance.request_metrics import merge_request_measurements
from benchmarks.performance.wandb_results import wandb_group_name

logger = logging.getLogger(__name__)


def _dataset_directory_name(index: int, dataset_selector: str) -> str:
    safe_name = re.sub(r"[^\w.\-]+", "_", dataset_selector).strip("_")
    return f"{index:02d}_{safe_name or 'dataset'}"


def _allocate_request_counts(
    request_count: int,
    dataset_count: int,
) -> list[int]:
    """按数据集顺序尽量均匀地分配总请求数。"""
    base_count, remainder = divmod(request_count, dataset_count)
    return [
        base_count + (1 if index < remainder else 0)
        for index in range(dataset_count)
    ]


class MultiDatasetBenchmark:
    """拥有多数据集请求分配、子负载顺序和合并结果。"""

    def __init__(self, benchmark: HttpBenchmarkConfig) -> None:
        self.benchmark = benchmark

    async def run(self) -> dict[str, Any]:
        """依次评测各数据集并发布一次兼容的合并性能结果。"""
        load_record = resolved_load_record(self.benchmark)
        dataset_selectors = list(
            self.benchmark.request_dataset.dataset_selectors
        )
        total_requests = int(load_record["number"])
        request_counts = _allocate_request_counts(
            total_requests, len(dataset_selectors)
        )
        result_directory = open_local_result_directory(self.benchmark)
        run_record = build_benchmark_run_record(
            self.benchmark,
            "multi_dataset",
            load_record,
        )
        run_record["datasets"] = dataset_selectors
        run_record["dataset_request_counts"] = request_counts

        wandb_enabled = self.benchmark.outputs.includes("wandb")
        wandb_group = (
            wandb_group_name(self.benchmark) if wandb_enabled else None
        )

        dataset_measurements: list[dict[str, Any]] = []
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
                request_dataset=replace(
                    self.benchmark.request_dataset,
                    dataset_selectors=[dataset_selector],
                ),
                load_schedule=replace(
                    self.benchmark.load_schedule,
                    request_count=request_count,
                ),
            )
            child_result = await StandardHttpLoadBenchmark(
                child_benchmark,
                label=child_name,
                output_dir=os.path.join(
                    result_directory.output_dir, child_name
                ),
                wandb_group=wandb_group,
                collect_request_measurements=True,
            ).run()
            dataset_measurements.append(child_result["raw"])
            dataset_results.append(
                {
                    "dataset": dataset_selector,
                    "number": request_count,
                    "metrics": child_result["metrics"],
                    "output_dir": child_result["output_dir"],
                }
            )

        if not dataset_measurements:
            raise ValueError(
                f"No requests dispatched for datasets={dataset_selectors} "
                f"with total number={total_requests}"
            )

        merged_measurements = merge_request_measurements(dataset_measurements)
        metrics = summarize_http_measurements(
            self.benchmark,
            merged_measurements,
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
                "max_turns": self.benchmark.request_dataset.max_turns,
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
        publish_benchmark_results(
            self.benchmark,
            result_directory,
            run_record,
            merged_measurements,
            metrics,
        )

        return {
            "mode": "multi_dataset",
            "metrics": metrics,
            "output_dir": result_directory.output_dir,
            "datasets": dataset_selectors,
            "dataset_request_counts": request_counts,
            "wandb_group": wandb_group,
        }
