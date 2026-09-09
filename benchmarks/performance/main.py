# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Current ``foretoken bench`` HTTP benchmark entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any

from benchmarks.performance.cli import parse_http_benchmark_arguments
from benchmarks.performance.arrival_trace_benchmark import ArrivalTraceBenchmark
from benchmarks.performance.config import HttpBenchmarkConfig
from benchmarks.performance.console_output import (
    configure_logging,
    format_benchmark_config,
    print_benchmark_endpoint,
)
from benchmarks.performance.deployment import (
    BenchmarkRuntimeEndpoint,
    benchmark_endpoint_from_kustomize,
    direct_benchmark_endpoint,
)
from benchmarks.performance.http_benchmark import StandardHttpLoadBenchmark
from benchmarks.performance.multi_dataset import MultiDatasetBenchmark
from benchmarks.performance.sweep import ParameterSweepBenchmark
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


async def run_http_benchmark(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
) -> dict[str, Any]:
    """Select and run the single workload lifecycle for the current HTTP benchmark configuration."""
    benchmark.validate()
    if benchmark.arrival_trace.trace_selector:
        return await ArrivalTraceBenchmark(benchmark, endpoint).run()
    if benchmark.parameter_sweep.bench_params:
        return await ParameterSweepBenchmark(benchmark, endpoint).run()
    if benchmark.request_dataset.has_multiple_datasets:
        return await MultiDatasetBenchmark(benchmark, endpoint).run()
    return await StandardHttpLoadBenchmark(benchmark, endpoint).run()


def main(argv: Sequence[str] | None = None) -> None:
    """Parse and run the current HTTP benchmark for lazy import by the top-level CLI."""
    try:
        benchmark = parse_http_benchmark_arguments(argv)
        benchmark.validate()
        configure_logging(not benchmark.outputs.includes("quiet"))
        deployment_path = benchmark.deployment.kustomize_path
        if deployment_path:
            service_context = benchmark_endpoint_from_kustomize(
                deployment_path,
                benchmark.deployment.wait_timeout,
                requested_model=benchmark.endpoint.model,
                api_key=benchmark.endpoint.api_key,
            )
        else:
            service_context = nullcontext(
                direct_benchmark_endpoint(benchmark.endpoint)
            )
        with service_context as endpoint:
            if deployment_path and not benchmark.outputs.includes("quiet"):
                print_benchmark_endpoint(
                    endpoint.url,
                    endpoint.models,
                    endpoint.hostname,
                )

            logger.info("%s", format_benchmark_config(benchmark, endpoint))
            result = asyncio.run(run_http_benchmark(benchmark, endpoint))
            if result["metrics"]["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
