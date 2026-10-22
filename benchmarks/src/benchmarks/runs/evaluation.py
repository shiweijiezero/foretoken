# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native scoring inside the shared service and result lifecycles."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

from foretoken.processes.execution import CommandExecution
from loguru import logger

from benchmarks.config.evaluation import EvaluationConfig, deployment_labels
from benchmarks.model_service import ModelService, ModelServiceResolver
from benchmarks.results.output import ResultOutputs
from benchmarks.results.reporting.evaluation import (
    evaluation_comparison_sinks,
    evaluation_sinks,
)
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.recording import wandb_run_timestamp


def run_evaluation(
    config: EvaluationConfig,
    service: ModelService,
    *,
    output_dir: str | None = None,
    outputs: ResultOutputs | None = None,
) -> BenchmarkRun:
    """Publish one evaluation, borrowing entered outputs when the caller owns service cleanup."""
    if config.evaluator == "lm-eval":
        from benchmarks.integrations.lm_eval.reports import read_metrics
    else:
        from benchmarks.integrations.evalscope.reports import read_metrics

    record: dict[str, Any] = {
        "mode": "evaluation",
        "evaluator": config.evaluator,
        "model": service.model,
    }
    output_context = (
        nullcontext(outputs)
        if outputs is not None
        else ResultOutputs(config, service, directory_prefix="eval-", output_dir=output_dir)
    )
    with output_context as active_outputs:
        active_outputs.service = service
        active_outputs.sink_factory = lambda directory: evaluation_sinks(config, record, directory)
        active_outputs.open(record)
        directory = Path(active_outputs.execution_dir).resolve()
        logger.info(
            "Evaluation: {} | model={} | artifacts={}",
            config.evaluator,
            service.model,
            directory,
        )
        progress_writer = active_outputs.create_request_writer("evaluation_progress")

        def publish_progress(row: dict[str, Any]) -> None:
            """Retain each measured snapshot before sending it to live destinations."""
            progress_writer.write(row)
            active_outputs.publish_progress(row, axis="Evaluation/Elapsed (s)")

        started = time.monotonic()
        run: BenchmarkRun

        def measured(code: int) -> None:
            """Commit native scores and their outcome before progress and process cleanup."""
            nonlocal run
            metrics = read_metrics(directory / "native")
            run = BenchmarkRun(
                record=record,
                metrics={**metrics, "duration_seconds": time.monotonic() - started},
                measurements=None,
                artifacts={"native": directory / "native", "log": directory / "evaluator.log"},
                exit_code=code,
            )
            active_outputs.save_measurement(run)

        code = _execute(config, service, directory, publish_progress, active_outputs, measured)
        active_outputs.publish(run)
        if code:
            logger.error(
                "{} failed (exit {}); see {}", config.evaluator, code, directory / "evaluator.log"
            )
        return run


def run_evaluation_comparison(config: EvaluationConfig) -> None:
    """Score each deployment sequentially and publish one aligned task comparison."""
    config = replace(config, wandb=replace(
        config.wandb, group=config.wandb.group or f"eval-comparison_{wandb_run_timestamp()}"
    ))
    record = {"mode": "evaluation_comparison", "evaluator": config.evaluator}
    scores: list[dict[str, Any]] = []
    execution: dict[str, Any] = {}
    methods = [
        {"label": label, "model": source.model or None, "directory": f"method-{index + 1}",
         "exit_code": None, "status": "not_started"}
        for index, (source, label) in enumerate(zip(config.services, deployment_labels(config.services)))
    ]
    started = time.monotonic()
    with ResultOutputs(
        config, None, directory_prefix="eval-comparison-",
        sink_factory=lambda directory: evaluation_comparison_sinks(config, record, directory),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir)
        child_config = replace(config, outputs=config.outputs.for_child_run())
        complete = False
        resolver = ModelServiceResolver()
        try:
            for source, method in zip(config.services, methods):
                method["status"] = "running"
                with resolver.resolve(source) as service:
                    method["model"] = service.model
                    run = run_evaluation(child_config, service, output_dir=str(directory / method["directory"]))
                    method["exit_code"] = run.exit_code
                    method["status"] = "failed" if run.exit_code else "completed"
                    execution[method["label"]] = run.metrics["execution"]
                    scores.extend({"method": method["label"], **row} for row in run.metrics["scores"])
            complete = True
        finally:
            for method in methods:
                if method["status"] == "running":
                    method["status"] = "incomplete"
            failure = not complete or any(method["exit_code"] for method in methods)
            metrics = {
                "duration_seconds": time.monotonic() - started,
                "scores": scores,
                "execution": {
                    "evaluation_comparison": {
                        "requested": len(config.services),
                        "succeeded": sum(method["status"] == "completed" for method in methods),
                        "errored": sum(method["status"] == "failed" for method in methods),
                        "incomplete": failure or any(
                            task.get("incomplete")
                            for native in execution.values() for task in native.values()
                        ),
                    },
                    "methods": execution,
                },
                "evaluation_comparison": {"methods": methods, "scores": scores},
            }
            try:
                outputs.publish(BenchmarkRun(
                    record, metrics, None,
                    {f"method-{index + 1}": path for index, method in enumerate(methods)
                     if (path := directory / method["directory"] / "native").is_dir()},
                    exit_code=int(failure),
                ))
            except BaseException:
                if complete:
                    raise
                logger.exception("Could not publish the evaluation comparison")
        if failure:
            raise SystemExit(1)


def _poll_progress(
    evaluator: str, native: Path, stop: Event, publish: Callable[[dict[str, Any]], None],
) -> None:
    """Observe adapter completion counts independently of evaluator stdout and flush on shutdown."""
    started = time.monotonic()
    previous = None
    if evaluator == "lm-eval":
        from benchmarks.integrations.quality import read_progress
    else:
        from benchmarks.integrations.evalscope.progress import EvalScopeProgress

        read_progress = EvalScopeProgress().read
    while True:
        # A stop received during publication still requires a final snapshot read.
        stopping = stop.is_set()
        counts = read_progress(native)
        if counts is not None and counts != previous:
            publish({"Evaluation/Elapsed (s)": time.monotonic() - started, **counts})
            previous = counts
        if stopping:
            return
        stop.wait(1)


def _execute(
    config: EvaluationConfig, service: ModelService, directory: Path,
    publish_progress: Callable[[dict[str, Any]], None],
    outputs: ResultOutputs,
    on_complete: Callable[[int], None],
) -> int:
    """Stream native progress into a retained log; interrupt and reap the child before service cleanup."""
    native = directory / "native"
    native.mkdir()
    payload = {
        "evaluator": config.evaluator,
        "arguments": config.arguments,
        "directory": str(native),
        "resume": config.resume,
        "service": {
            "model": service.model,
            "api_key": service.api_key,
            "chat_url": service.chat_completions_url,
            "api_root": service.api_root,
            "headers": service.request_headers,
            **({"tokenizer_identity": service.tokenizer_identity} if config.evaluator == "lm-eval" else {}),
        },
    }
    # Keep the caller's cwd: upstream config, cache and task paths remain relative to it.
    with (
        (directory / "evaluator.log").open("w", encoding="utf-8") as log,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="evaluation-progress") as executor,
        (CommandExecution.current() or CommandExecution()) as execution,
        execution.process(
            [sys.executable, "-u", "-m", "benchmarks.integrations.quality"],
            interrupt_signal=signal.SIGINT,
            shutdown_grace=10,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process,
    ):
        stop = Event()
        progress = None
        active_error: BaseException | None = None
        execution.register(stop.set)
        try:
            # Progress failure interrupts the owned process even while stdout is silent.
            progress = executor.submit(
                execution.run_stage, _poll_progress,
                config.evaluator, native, stop, publish_progress,
            )
            outputs.start_measurement()
            input_error = None
            try:
                process.stdin.write(json.dumps(payload))
                process.stdin.close()
            except BrokenPipeError as error:
                execution.check()
                # An early native exit can close stdin before its diagnostic output is read.
                input_error = error
            for line in process.stdout:
                execution.check()
                if service.api_key and service.api_key != "EMPTY":
                    line = line.replace(service.api_key, "[redacted]")
                log.write(line)
                log.flush()
                print(line, end="", file=sys.stderr, flush=True)
            code = process.wait()
            execution.check()
            if input_error is not None and code == 0:
                raise input_error
            on_complete(code)
            return code
        except BaseException as error:
            active_error = error
            execution.cancel(error)
            raise
        finally:
            stop.set()
            try:
                if progress is not None:
                    progress.result()
            except BaseException as progress_error:
                if active_error is None:
                    raise
                if progress_error is not active_error:
                    logger.exception("Evaluation progress cleanup also failed")
            finally:
                execution.unregister(stop.set)
