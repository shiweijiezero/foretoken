# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Current ``foretoken bench`` HTTP benchmark entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any

from benchmarks.config.cli import parse_http_benchmark_arguments
from benchmarks.load.trace import ArrivalTraceBenchmark
from benchmarks.config import HttpBenchmarkConfig
from benchmarks.reporting.console import (
    configure_logging,
    format_benchmark_config,
    print_benchmark_endpoint,
)
from benchmarks.deployment import (
    BenchmarkRuntimeEndpoint,
    benchmark_endpoint_from_kustomize,
    direct_benchmark_endpoint,
)
from benchmarks.load.standard import StandardHttpLoadBenchmark
from benchmarks.experiments.multi_dataset import MultiDatasetBenchmark
from benchmarks.experiments.sweep import ParameterSweepBenchmark
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


def run_http_benchmark(
    benchmark: HttpBenchmarkConfig,
    endpoint: BenchmarkRuntimeEndpoint,
) -> dict[str, Any]:
    """Select and run the single workload lifecycle for the current HTTP benchmark configuration."""
    benchmark.validate()
    if benchmark.arrival_trace.trace_selector:
        return asyncio.run(ArrivalTraceBenchmark(benchmark, endpoint).run())
    if benchmark.parameter_sweep.bench_params:
        return ParameterSweepBenchmark(benchmark, endpoint).run()
    if benchmark.resolved_dataset.has_multiple_datasets:
        return MultiDatasetBenchmark(benchmark, endpoint).run()
    return StandardHttpLoadBenchmark(benchmark, endpoint).run()


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
            result = run_http_benchmark(benchmark, endpoint)
            if result["metrics"]["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
