# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""``foretoken bench`` HTTP benchmark entry point."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.config.cli import parse_benchmark_arguments
from benchmarks.episodes.generated_load import GeneratedLoadBenchmark
from benchmarks.episodes.trace_replay import TraceReplayBenchmark
from benchmarks.model_service import ModelService, resolve_model_service
from benchmarks.multi_dataset import MultiDatasetBenchmark
from benchmarks.results.console import (
    configure_logging,
    format_benchmark_config,
    print_model_service,
)
from benchmarks.sweep import ParameterSweepBenchmark
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


def select_benchmark(
    benchmark: BenchmarkConfig,
    service: ModelService,
) -> (
    TraceReplayBenchmark
    | ParameterSweepBenchmark
    | MultiDatasetBenchmark
    | GeneratedLoadBenchmark
):
    """Choose the benchmark that owns the configured workload; its ``run()`` returns a ``BenchmarkRun``."""
    if benchmark.trace.trace_selector:
        return TraceReplayBenchmark(benchmark, service)
    if benchmark.sweep.path:
        return ParameterSweepBenchmark(benchmark, service)
    if benchmark.resolved_workload.has_multiple_datasets:
        return MultiDatasetBenchmark(benchmark, service)
    return GeneratedLoadBenchmark(benchmark, service)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse and run one benchmark command for lazy import by the top-level CLI."""
    try:
        benchmark = parse_benchmark_arguments(argv)
        benchmark.validate()
        quiet = benchmark.outputs.includes("quiet")
        configure_logging(not quiet)
        with resolve_model_service(benchmark.service) as service:
            if benchmark.service.kustomize_path and not quiet:
                print_model_service(service)

            logger.info("%s", format_benchmark_config(benchmark, service))
            run = select_benchmark(benchmark, service).run()
            if run.metrics["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
