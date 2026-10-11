# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native scoring inside the shared service and result lifecycles."""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from benchmarks.config.evaluation import EvaluationConfig, deployment_labels
from benchmarks.model_service import ModelService, resolve_model_service
from benchmarks.results.evaluation import (
    evaluation_comparison_sinks,
    evaluation_sinks,
    read_quality_metrics,
)
from benchmarks.results.output import BenchmarkRun, ResultOutputs, wandb_run_timestamp

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
    with (
        (directory / "evaluator.log").open("w", encoding="utf-8") as log,
        subprocess.Popen(
            [sys.executable, "-u", "-m", "benchmarks.integrations.quality"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process,
    ):
        try:
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
            for line in process.stdout:
                if service.api_key and service.api_key != "EMPTY":
                    line = line.replace(service.api_key, "[redacted]")
                log.write(line)
                log.flush()
                if not config.outputs.includes("quiet"):
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


def run_evaluation(
    config: EvaluationConfig, service: ModelService, *, output_dir: str | None = None
) -> BenchmarkRun:
    """Execute one evaluator and publish scores; a comparison owns its child failure status."""
    record: dict[str, Any] = {
        "mode": "evaluation",
        "evaluator": config.evaluator,
        "model": service.model,
    }
    with ResultOutputs(
        config,
        service,
        directory_prefix="eval-",
        output_dir=output_dir,
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
        return run


def run_evaluation_comparison(config: EvaluationConfig) -> None:
    """Score each deployment sequentially and publish one aligned task comparison."""
    config = replace(config, wandb=replace(
        config.wandb, group=config.wandb.group or f"eval-comparison_{wandb_run_timestamp()}"
    ))
    record = {"mode": "evaluation_comparison", "evaluator": config.evaluator}
    scores: list[dict[str, Any]] = []
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
        try:
            for source, method in zip(config.services, methods):
                method["status"] = "running"
                with resolve_model_service(source) as service:
                    method["model"] = service.model
                    run = run_evaluation(child_config, service, output_dir=str(directory / method["directory"]))
                    method["exit_code"] = run.exit_code
                    method["status"] = "failed" if run.exit_code else "completed"
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
                "execution": {"evaluation_comparison": {
                    "requested": len(config.services),
                    "succeeded": sum(method["status"] == "completed" for method in methods),
                    "errored": sum(method["status"] == "failed" for method in methods),
                    "incomplete": not complete,
                }},
                "evaluation_comparison": {"methods": methods, "scores": scores},
            }
            outputs.publish(BenchmarkRun(
                record, metrics, None,
                {f"method-{index + 1}": path for index, method in enumerate(methods)
                 if (path := directory / method["directory"] / "native").is_dir()},
                exit_code=int(failure),
            ))
        if failure:
            raise SystemExit(1)
