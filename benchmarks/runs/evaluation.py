# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run text and video quality evaluators inside shared result lifecycles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.integrations.video_evaluator import VideoEvaluator
from benchmarks.model_service import ModelService
from benchmarks.results.evaluation import (
    EvaluationResultConfig,
    evaluation_sinks,
    read_quality_metrics,
)
from benchmarks.results.output import BenchmarkRun, ResultOutputs

logger = logging.getLogger(__name__)


def run_logged_process(
    command: Sequence[str],
    log_path: Path,
    *,
    quiet: bool,
    cwd: str | None = None,
    environment: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    redactions: Sequence[str] = (),
) -> int:
    """Stream an evaluator child to a log and reap it before caller cleanup."""
    child_environment = os.environ.copy()
    if environment:
        child_environment.update(environment)
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            list(command),
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process:
            try:
                if stdin_text is not None:
                    process.stdin.write(stdin_text)
                    process.stdin.close()
                for line in process.stdout:
                    for secret in redactions:
                        if secret:
                            line = line.replace(secret, "[redacted]")
                    log.write(line)
                    log.flush()
                    if not quiet:
                        print(line, end="", flush=True)
                return process.wait()
            except BaseException:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise


def _execute(config: EvaluationConfig, service: ModelService, directory: Path) -> int:
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
    redactions = (
        (service.api_key,)
        if service.api_key and service.api_key != "EMPTY"
        else ()
    )
    return run_logged_process(
        [sys.executable, "-u", "-m", "benchmarks.integrations.quality"],
        directory / "evaluator.log",
        quiet=config.outputs.includes("quiet"),
        stdin_text=json.dumps(payload),
        redactions=redactions,
    )


def run_evaluation(config: EvaluationConfig, service: ModelService) -> None:
    """Execute one evaluator and publish all available scores, including partial failed runs."""
    record: dict[str, Any] = {
        "mode": "evaluation",
        "evaluator": config.evaluator,
        "model": service.model,
    }
    with ResultOutputs(
        config,
        service,
        directory_prefix="eval-",
        sink_factory=lambda directory: evaluation_sinks(config, record, directory),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir).resolve()
        logger.info(
            "Evaluation: %s | model=%s | artifacts=%s",
            config.evaluator,
            service.model,
            directory,
        )
        started = time.monotonic()
        code = _execute(config, service, directory)
        metrics = read_quality_metrics(config.evaluator, directory / "native")
        run = BenchmarkRun(
            record=record,
            metrics={**metrics, "duration_seconds": time.monotonic() - started},
            measurements=None,
            artifacts={
                "native": directory / "native",
                "log": directory / "evaluator.log",
            },
            exit_code=code,
        )
        outputs.publish(run)
        if code:
            logger.error(
                "%s failed (exit %s); see %s", config.evaluator, code, directory / "evaluator.log"
            )
            raise SystemExit(code if code > 0 else 128 - code)


def run_video_evaluation(
    config: EvaluationResultConfig, evaluator: VideoEvaluator
) -> None:
    """Execute one video adapter and publish its scores and run metadata."""
    identity = evaluator.describe()
    record: dict[str, Any] = {
        "mode": "evaluation",
        "evaluator": evaluator.name,
        "evaluation_mode": identity.evaluation_mode,
        "model": identity.model,
        **identity.metadata,
    }
    with ResultOutputs(
        config,
        None,
        directory_prefix="eval-video-",
        sink_factory=lambda directory: evaluation_sinks(config, record, directory),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir).resolve()
        native = directory / "native"
        native.mkdir()
        invocation = evaluator.prepare(native)
        evaluator_log = directory / "evaluator.log"
        logger.info(
            "Evaluation: %s | model=%s | artifacts=%s",
            evaluator.name,
            record["model"],
            directory,
        )
        started = time.monotonic()
        code = run_logged_process(
            invocation.arguments,
            evaluator_log,
            quiet=config.outputs.includes("quiet"),
            cwd=invocation.cwd,
            environment=invocation.environment,
        )
        metrics = evaluator.read_metrics(native)
        if code == 0 and not metrics["scores"]:
            logger.error("%s produced no scores; see %s", evaluator.name, evaluator_log)
            code = 1
        run = BenchmarkRun(
            record=record,
            metrics={**metrics, "duration_seconds": time.monotonic() - started},
            measurements=None,
            artifacts={"native": native, "log": evaluator_log},
            exit_code=code,
        )
        outputs.publish(run)
        if code:
            logger.error(
                "%s failed (exit %s); see %s", evaluator.name, code, evaluator_log
            )
            raise SystemExit(code if code > 0 else 128 - code)
