# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""``foretoken bench`` HTTP benchmark entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.config.cli import parse_benchmark_arguments
from benchmarks.config.video_cli import parse_video_arguments
from benchmarks.datasets.multi_dataset import MultiDatasetBenchmark
from benchmarks.model_service import ModelService, resolve_model_service
from benchmarks.results.console import (
    configure_logging,
    format_benchmark_config,
    print_model_service,
)
from benchmarks.runs.http import GeneratedLoadBenchmark, run_http_dataset
from benchmarks.runs.sweep import ParameterSweepBenchmark
from benchmarks.runs.trace import TraceReplayBenchmark
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
        return MultiDatasetBenchmark(benchmark, service, run_http_dataset)
    return GeneratedLoadBenchmark(benchmark, service)


def _run_video(arguments: Sequence[str], *, command_name: str) -> None:
    """Parse and run one request-only video-generation benchmark."""
    from benchmarks.runs.video import VideoBenchmarkError, run_video_benchmark

    try:
        command = parse_video_arguments(arguments, command_name=command_name)
        config = command.config
        configure_logging(not config.outputs.includes("quiet"))
        result = asyncio.run(run_video_benchmark(config, dry_run=command.dry_run))
    except (VideoBenchmarkError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if not result.get("dry_run") and result["metrics"]["success_num"] == 0:
        raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse and run one benchmark command for lazy import by the top-level CLI."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ("video",):
        _run_video(arguments[1:], command_name=arguments[0])
        return
    try:
        benchmark = parse_benchmark_arguments(arguments)
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
