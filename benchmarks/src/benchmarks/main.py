# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch serving-performance workloads through their measurement owners."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foretoken.diagnostics import configure_logging
from foretoken.errors import DeploymentError
from loguru import logger

from benchmarks.config.benchmark import BenchmarkConfig, BenchmarkOutputConfig
from benchmarks.config.cli import parse_benchmark_arguments
from benchmarks.results.recording.database import read_record

if TYPE_CHECKING:
    from benchmarks.model_service import ModelService
    from benchmarks.results.output import ResultOutputs
    from benchmarks.results.types import BenchmarkRun


def main(argv: Sequence[str] | None = None) -> None:
    """Parse and run one benchmark command for lazy import by the top-level CLI."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if "--agentx" in options:
        _run_agentx(arguments)
        return
    if "--video" in options:
        _run_video(arguments)
        return
    # Help and argument errors do not require measurement or publication backends.
    try:
        benchmark = parse_benchmark_arguments(arguments)
    except (DeploymentError, OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    import wandb

    from benchmarks.model_service import ModelServiceResolver
    from benchmarks.results.experiment import experiment_output
    from benchmarks.results.output import ResultOutputs
    from benchmarks.results.reporting.http import format_benchmark_config, log_model_service

    try:
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
                from benchmarks.sweeps.http import ParameterSweepBenchmark

                run = ParameterSweepBenchmark(benchmark).run()
            else:
                benchmark.validate()
                resolver = ModelServiceResolver()
                with ResultOutputs(benchmark, None) as outputs, ExitStack() as services:
                    def prepare_service() -> ModelService:
                        """Keep serving resources in the command scope, including deferred SLO probes."""
                        service = services.enter_context(resolver.benchmark(benchmark))
                        if benchmark.service.kustomize_path:
                            log_model_service(service)
                        logger.info("{}", format_benchmark_config(benchmark, service))
                        return service

                    run = run_benchmark(benchmark, prepare_service, outputs=outputs)
            if run.exit_code:
                raise SystemExit(run.exit_code)
    except (DeploymentError, OSError, sqlite3.Error, ValueError, wandb.errors.Error) as exc:
        raise SystemExit(str(exc)) from exc


def run_benchmark(
    benchmark: BenchmarkConfig,
    prepare_service: Callable[[], ModelService],
    *,
    outputs: ResultOutputs | None = None,
) -> BenchmarkRun:
    """Execute a measurement or search, acquiring the service only for new measurements."""
    if benchmark.slo.search:
        from benchmarks.runs.slo import SloAutoTuneBenchmark

        return SloAutoTuneBenchmark(benchmark, prepare_service, outputs=outputs).run()
    from benchmarks.runs.http import HttpBenchmark

    return HttpBenchmark(benchmark, prepare_service(), outputs=outputs).run()


def _run_video(arguments: Sequence[str]) -> None:
    """Parse and run one request-only video-generation benchmark."""
    from benchmarks.config.video_cli import parse_video_arguments

    try:
        command = parse_video_arguments(arguments)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    import wandb

    from benchmarks.results.experiment import experiment_output

    try:
        config = command.config
        configure_logging(not config.outputs.includes("quiet"))
        recovered = _recover_completed(config.sweep.resume, config.outputs, arguments, config.to_dict())
        if recovered is not None:
            if recovered.exit_code:
                raise SystemExit(recovered.exit_code)
            return
        with experiment_output(config, "perf", arguments) as config:
            if config.sweep.path:
                from benchmarks.sweeps.video import run_video_sweep

                result = run_video_sweep(config, dry_run=command.dry_run)
            else:
                from benchmarks.runs.video import run_video_benchmark

                result = asyncio.run(run_video_benchmark(config, dry_run=command.dry_run))
            if result["exit_code"]:
                raise SystemExit(result["exit_code"])
    except (OSError, sqlite3.Error, ValueError, wandb.errors.Error) as exc:
        raise SystemExit(str(exc)) from exc


def _run_agentx(arguments: Sequence[str]) -> None:
    """Run the official replay with the existing service, experiment, and sweep owners."""
    from benchmarks.config.agentx import parse_agentx_arguments
    from benchmarks.integrations.agentx import AgentXExecutionError

    try:
        config = parse_agentx_arguments(arguments)
    except (DeploymentError, OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit(str(error)) from error

    import wandb

    from benchmarks.model_service import ModelServiceResolver
    from benchmarks.results.experiment import experiment_output
    from benchmarks.runs.agentx import run_agentx

    configure_logging(not config.outputs.includes("quiet"))
    try:
        recovered = _recover_completed(config.sweep.resume, config.outputs, arguments, config.to_dict())
        if recovered is not None:
            if recovered.exit_code:
                raise SystemExit(recovered.exit_code)
            return
        with experiment_output(config, "perf", arguments) as config:
            if config.sweep.path or len(config.services) > 1 or config.sweep.num_runs > 1:
                from benchmarks.sweeps.agentx import run_agentx_sweep

                run = run_agentx_sweep(config)
            else:
                with ModelServiceResolver().resolve(config.service) as service:
                    run = run_agentx(config, service)
            if run.exit_code:
                raise SystemExit(run.exit_code)
    except (AgentXExecutionError, DeploymentError, OSError, sqlite3.Error, ValueError, wandb.errors.Error) as error:
        raise SystemExit(str(error)) from error


def _recover_completed(
    resume: str, outputs: BenchmarkOutputConfig, arguments: Sequence[str], configuration: dict[str, Any],
) -> BenchmarkRun | None:
    """Recover completed measurement outputs before acquiring a model service."""
    if not resume:
        return None
    from benchmarks.results.recording.locks import publication_lock
    from benchmarks.results.recording.recovery import measurement_complete
    from benchmarks.results.recovery import recover_results

    source = Path(resume).resolve()
    with publication_lock(source):
        context = read_record(source, "context.json")
        if not measurement_complete(source, context):
            return None
        saved = read_record(source, "config.json")
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
