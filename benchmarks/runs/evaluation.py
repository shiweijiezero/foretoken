# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native scoring inside the shared service and result lifecycles."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.model_service import ModelService
from benchmarks.results.evaluation import evaluation_sinks, read_quality_metrics
from benchmarks.results.output import BenchmarkRun, ResultOutputs
from benchmarks.runs.native import run_logged_process

logger = logging.getLogger(__name__)


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
