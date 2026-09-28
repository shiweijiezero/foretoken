# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""``foretoken perf`` HTTP performance benchmark entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence

import wandb
from foretoken.manifest import DeploymentError

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.config.cli import parse_benchmark_arguments
from benchmarks.config.video_cli import parse_video_arguments
from benchmarks.model_service import ModelService, resolve_benchmark_service
from benchmarks.results.console import (
    configure_logging,
    format_benchmark_config,
    print_model_service,
)
from benchmarks.results.output import BenchmarkRun
from benchmarks.runs.dispatch import measurement_runner
from benchmarks.runs.slo import SloAutoTuneBenchmark
from benchmarks.runs.video import run_video_benchmark
from benchmarks.sweeps.http import ParameterSweepBenchmark
from benchmarks.sweeps.video import run_video_sweep

logger = logging.getLogger(__name__)


def run_benchmark(
    benchmark: BenchmarkConfig,
    service: ModelService,
) -> BenchmarkRun:
    """Execute an SLO search or measurement on a prepared service."""
    if benchmark.slo.params:
        return SloAutoTuneBenchmark(benchmark, service).run()
    return measurement_runner(benchmark, service).run()


def _run_video(arguments: Sequence[str], *, command_name: str) -> None:
    """Parse and run one request-only video-generation benchmark."""
    try:
        command = parse_video_arguments(arguments, command_name=command_name)
        config = command.config
        configure_logging(not config.outputs.includes("quiet"))
        result = (
            run_video_sweep(config, dry_run=command.dry_run)
            if config.sweep.path
            else asyncio.run(run_video_benchmark(config, dry_run=command.dry_run))
        )
    except (ValueError, wandb.errors.Error) as exc:
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
        quiet = benchmark.outputs.includes("quiet")
        configure_logging(not quiet)
        if benchmark.sweep.path or len(benchmark.service_choices) > 1:
            run = ParameterSweepBenchmark(benchmark).run()
        else:
            benchmark.validate()
            with resolve_benchmark_service(benchmark) as service:
                if benchmark.service.kustomize_path and not quiet:
                    print_model_service(service)
                logger.info("%s", format_benchmark_config(benchmark, service))
                run = run_benchmark(benchmark, service)
        if run.metrics["success_num"] == 0:
            raise SystemExit(1)
    except (DeploymentError, ValueError, wandb.errors.Error) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
