# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""执行一个标准 OpenAI-compatible HTTP 性能负载。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from tempfile import TemporaryDirectory
from typing import Any, Optional

from tqdm.asyncio import tqdm as tqdm_asyncio

from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.chat_client import (
    ChatCompletionsLoadClient,
    ChatRequestContent,
)
from benchmarks.performance.console_output import log_benchmark_summary
from benchmarks.performance.evalscope_load import (
    run_evalscope_standard_load,
    uses_evalscope_standard_load,
)
from benchmarks.performance.local_results import LocalResultDirectory
from benchmarks.performance.request_datasets import load_chat_requests
from benchmarks.performance.request_metrics import (
    attach_user_throughput,
    summarize_request_measurements,
)
from benchmarks.performance.wandb_results import WandbBenchmarkRun

logger = logging.getLogger(__name__)


def resolved_load_record(benchmark: HttpBenchmarkConfig) -> dict[str, Any]:
    """把内部负载配置映射为既有结果字段使用的标量字典。"""
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
    mode: str,
    load_record: dict[str, Any],
) -> dict[str, Any]:
    """构造控制台和本地结果共同使用的单次运行记录。"""
    record = {
        "mode": mode,
        "model": benchmark.endpoint.model,
        "url": benchmark.endpoint.url,
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
    if benchmark.request_dataset.is_multi_turn:
        record["multi_turn"] = True
        record["max_turns"] = benchmark.request_dataset.max_turns
    if benchmark.request_dataset.dataset_selectors == ["random"]:
        record["random_seed"] = benchmark.request_dataset.random_seed
    return record


def open_local_result_directory(
    benchmark: HttpBenchmarkConfig,
    output_dir: Optional[str] = None,
) -> LocalResultDirectory:
    """为一次运行或实验根目录解析唯一的本地结果目录。"""
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
    """聚合逐请求观测并附加该次负载的公开坐标。"""
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
    """按当前输出选择发布控制台、JSON 和 W&B 性能结果。"""
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


async def dispatch_unbounded_requests(
    benchmark: HttpBenchmarkConfig,
    client: ChatCompletionsLoadClient,
    requests: list[ChatRequestContent],
) -> dict[str, Any]:
    """为 EvalScope 参数契约外的无限速 open-loop 立即派发全部请求。"""
    request_count = len(requests)
    results: list[Optional[dict[str, Any]]] = [None] * request_count
    started_at = time.perf_counter()
    progress_bar = tqdm_asyncio(
        total=request_count,
        desc="Benchmarking",
        disable=benchmark.outputs.includes("quiet"),
    )

    async def send_one(index: int, request: ChatRequestContent) -> None:
        try:
            measurement = await client.send(request)
            measurement["end_time"] = time.perf_counter() - started_at
            results[index] = measurement
        finally:
            progress_bar.update(1)

    try:
        await asyncio.gather(
            *[
                send_one(index, request)
                for index, request in enumerate(requests)
            ]
        )
    finally:
        progress_bar.close()

    logger.info("Benchmark finished!")
    if any(result is None for result in results):
        raise RuntimeError("dispatch finished with missing request results")
    return {
        "results": results,
        "total_time": time.perf_counter() - started_at,
    }


class StandardHttpLoadBenchmark:
    """拥有一次标准 HTTP 负载的请求、结果和外部 run 生命周期。"""

    def __init__(
        self,
        benchmark: HttpBenchmarkConfig,
        *,
        label: str = "",
        output_dir: Optional[str] = None,
        wandb_group: Optional[str] = None,
        collect_request_measurements: bool = False,
    ) -> None:
        self.benchmark = benchmark
        self.label = label
        self.output_dir = output_dir
        self.wandb_group = wandb_group
        self.collect_request_measurements = collect_request_measurements

    async def run(self) -> dict[str, Any]:
        """执行一次标准 HTTP 负载并返回既有结果字典。"""
        load_record = resolved_load_record(self.benchmark)
        result_directory = open_local_result_directory(
            self.benchmark, self.output_dir
        )
        run_record = build_benchmark_run_record(
            self.benchmark, "standard_load", load_record
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
                output_dir=execution_dir,
                parallel=int(load_record["resolved_parallel"]),
                rate=float(load_record["rate"]),
                name_suffix=label,
                group=self.wandb_group,
            )
            try:
                if uses_evalscope_standard_load(self.benchmark):
                    metrics, request_measurements = (
                        await run_evalscope_standard_load(
                            self.benchmark,
                            execution_dir,
                            collect_request_measurements=(
                                self.collect_request_measurements
                            ),
                        )
                    )
                else:
                    requests = load_chat_requests(self.benchmark)
                    async with ChatCompletionsLoadClient(
                        self.benchmark,
                        max_concurrency=load_record["parallel"],
                        request_count=load_record["number"],
                    ) as client:
                        request_measurements = await dispatch_unbounded_requests(
                            self.benchmark,
                            client,
                            requests,
                        )
                    metrics = summarize_http_measurements(
                        self.benchmark,
                        request_measurements,
                        arrival_rate=load_record["rate"],
                        request_count=load_record["number"],
                        reported_concurrency=load_record[
                            "resolved_parallel"
                        ],
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
