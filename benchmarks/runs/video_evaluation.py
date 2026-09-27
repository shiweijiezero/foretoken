# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run a video quality evaluator through shared result destinations."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any

from benchmarks.integrations.video_evaluator import VideoEvaluator
from benchmarks.results.evaluation import EvaluationResultConfig, evaluation_sinks
from benchmarks.results.output import BenchmarkRun, ResultOutputs
from benchmarks.runs.native import run_logged_process

logger = logging.getLogger(__name__)


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
