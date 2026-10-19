# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""``foretoken perf`` HTTP performance benchmark entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

import wandb
from foretoken.errors import DeploymentError

from benchmarks.config.benchmark import BenchmarkConfig, BenchmarkOutputConfig
from benchmarks.config.cli import parse_benchmark_arguments
from benchmarks.config.video_cli import parse_video_arguments
from benchmarks.model_service import ModelService, resolve_benchmark_service
from benchmarks.results.console import (
    configure_logging,
    format_benchmark_config,
    print_model_service,
)
from benchmarks.results.experiment import experiment_output
from benchmarks.results.output import BenchmarkRun, ResultOutputs
from benchmarks.runs.http import HttpBenchmark
from benchmarks.runs.slo import SloAutoTuneBenchmark
from benchmarks.runs.video import run_video_benchmark
from benchmarks.sweeps.http import ParameterSweepBenchmark
from benchmarks.sweeps.video import run_video_sweep

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse and run one benchmark command for lazy import by the top-level CLI."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if "--video" in options:
        _run_video(arguments)
        return
    try:
        benchmark = parse_benchmark_arguments(arguments)
        quiet = benchmark.outputs.includes("quiet")
        configure_logging(not quiet)
        benchmark.outputs.validate()
        recovered = _recover_completed(benchmark.sweep.resume, benchmark.outputs, arguments, benchmark.to_dict())
        if recovered is not None:
            if recovered.exit_code:
                raise SystemExit(recovered.exit_code)
            return
        with experiment_output(benchmark, "perf", arguments) as benchmark:
            if benchmark.sweep.path or len(benchmark.service_choices) > 1:
                run = ParameterSweepBenchmark(benchmark).run()
            else:
                benchmark.validate()
                with ResultOutputs(benchmark, None) as outputs, ExitStack() as services:
                    def prepare_service() -> ModelService:
                        """Keep serving resources in the command scope, including deferred SLO probes."""
                        service = services.enter_context(resolve_benchmark_service(benchmark))
                        if benchmark.service.kustomize_path and not quiet:
                            print_model_service(service)
                        logger.info("%s", format_benchmark_config(benchmark, service))
                        return service

                    run = run_benchmark(benchmark, prepare_service, outputs=outputs)
            if run.exit_code:
                raise SystemExit(run.exit_code)
    except (DeploymentError, OSError, ValueError, wandb.errors.Error) as exc:
        raise SystemExit(str(exc)) from exc


def run_benchmark(
    benchmark: BenchmarkConfig,
    prepare_service: Callable[[], ModelService],
    *,
    outputs: ResultOutputs | None = None,
) -> BenchmarkRun:
    """Execute a measurement or search, acquiring the service only for new measurements."""
    if benchmark.slo.search:
        return SloAutoTuneBenchmark(benchmark, prepare_service, outputs=outputs).run()
    return HttpBenchmark(benchmark, prepare_service(), outputs=outputs).run()


def _run_video(arguments: Sequence[str]) -> None:
    """Parse and run one request-only video-generation benchmark."""
    try:
        command = parse_video_arguments(arguments)
        config = command.config
        configure_logging(not config.outputs.includes("quiet"))
        recovered = _recover_completed(config.sweep.resume, config.outputs, arguments, config.to_dict())
        if recovered is not None:
            if recovered.exit_code:
                raise SystemExit(recovered.exit_code)
            return
        with experiment_output(config, "perf", arguments) as config:
            result = (
                run_video_sweep(config, dry_run=command.dry_run)
                if config.sweep.path
                else asyncio.run(run_video_benchmark(config, dry_run=command.dry_run))
            )
            if result["exit_code"]:
                raise SystemExit(result["exit_code"])
    except (OSError, ValueError, wandb.errors.Error) as exc:
        raise SystemExit(str(exc)) from exc


def _recover_completed(
    resume: str, outputs: BenchmarkOutputConfig, arguments: Sequence[str], configuration: dict[str, Any],
) -> BenchmarkRun | None:
    """Recover completed HTTP, video or composed results before any service acquisition."""
    if not resume:
        return None
    from benchmarks.results.publication import recover_results
    from benchmarks.results.wandb_sync import publication_lock

    source = Path(resume).resolve()
    with publication_lock(source):
        context = json.loads((source / "context.json").read_text())
    if context.get("measurement_exit_code") is None and not (
        context.get("phase") == "finalization" and (source / "metrics.json").is_file()
    ):
        return None
    saved = json.loads((source / "config.json").read_text())
    original = saved.get("base", saved)
    remaining = []
    options = iter(arguments)
    for argument in options:
        if argument in {"--resume", "--output", "--output-dir"}:
            next(options)
        elif not argument.startswith(("--resume=", "--output=", "--output-dir=")):
            remaining.append(argument)
    if remaining:
        current = json.loads(json.dumps(configuration))
        if any(current[key] != original.get(key) for key in current if key != "output"):
            raise ValueError("Resume uses the saved workload and W&B destination; omit measurement options or repeat the original settings")
    if not any(argument == "--output" or argument.startswith("--output=") for argument in arguments):
        outputs = replace(outputs, destinations=tuple(original["output"]["destinations"]))
    return recover_results(source, outputs)


if __name__ == "__main__":
    main()
