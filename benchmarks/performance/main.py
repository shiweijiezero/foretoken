# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""当前 ``foretoken bench`` HTTP 性能评测入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from benchmarks.performance.arguments import parse_http_benchmark_arguments
from benchmarks.performance.arrival_trace_benchmark import ArrivalTraceBenchmark
from benchmarks.performance.benchmark_config import HttpBenchmarkConfig
from benchmarks.performance.console_output import (
    configure_logging,
    format_benchmark_config,
    print_benchmark_endpoint,
)
from benchmarks.performance.deployment import benchmark_endpoint_from_kustomize
from benchmarks.performance.http_benchmark import StandardHttpLoadBenchmark
from benchmarks.performance.multi_dataset_benchmark import MultiDatasetBenchmark
from benchmarks.performance.parameter_sweep import ParameterSweepBenchmark
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


async def run_http_benchmark(
    benchmark: HttpBenchmarkConfig,
) -> dict[str, Any]:
    """按当前 HTTP 性能配置选择唯一的具体负载生命周期并运行。"""
    benchmark.validate()
    if benchmark.arrival_trace.trace_selector:
        return await ArrivalTraceBenchmark(benchmark).run()
    if benchmark.parameter_sweep.bench_params:
        return await ParameterSweepBenchmark(benchmark).run()
    if benchmark.request_dataset.has_multiple_datasets:
        return await MultiDatasetBenchmark(benchmark).run()
    return await StandardHttpLoadBenchmark(benchmark).run()


def main(argv: Sequence[str] | None = None) -> None:
    """解析并运行当前 HTTP 性能评测，供顶层 CLI 延迟导入。"""
    try:
        command = parse_http_benchmark_arguments(argv)
        benchmark = command.benchmark
        dataset = benchmark.request_dataset
        if (
            command.kustomize_path
            and not dataset.fixed_prompt
            and not dataset.dataset_selectors
        ):
            benchmark.request_dataset = replace(
                dataset,
                fixed_prompt="Hello",
            )
        benchmark.validate()
        if (
            benchmark.parameter_sweep.bench_params
            and not command.kustomize_path
        ):
            raise ValueError(
                "--bench-params requires a Foretoken Kustomize deployment"
            )
        configure_logging(not benchmark.outputs.includes("quiet"))
        if command.kustomize_path:
            service_context = benchmark_endpoint_from_kustomize(
                command.kustomize_path,
                command.wait_timeout,
                requested_model=benchmark.endpoint.model,
                api_key=benchmark.endpoint.api_key,
            )
        else:
            service_context = nullcontext(None)
        with service_context as endpoint:
            if endpoint is not None:
                benchmark.endpoint = replace(
                    benchmark.endpoint,
                    url=endpoint.url,
                    model=endpoint.model,
                    headers=endpoint.headers,
                )
                benchmark.serving_gpu_count = endpoint.gpu_count
                if not benchmark.outputs.includes("quiet"):
                    print_benchmark_endpoint(
                        endpoint.url,
                        endpoint.models,
                        endpoint.hostname,
                    )

            logger.info("%s", format_benchmark_config(benchmark))
            result = asyncio.run(run_http_benchmark(benchmark))
            if result["metrics"]["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
