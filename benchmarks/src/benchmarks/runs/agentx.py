# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execute the official AgentX client inside Foretoken's service and result lifecycles."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from foretoken.processes.execution import CommandExecution
from loguru import logger

from benchmarks.config.agentx import AgentXConfig
from benchmarks.integrations.agentx import HARNESS_VERSION, AgentXExecutionError
from benchmarks.integrations.agentx.reports import read_metrics
from benchmarks.integrations.agentx.runtime import profile_command
from benchmarks.model_service import ModelService, require_health_endpoint
from benchmarks.results.output import ResultOutputs
from benchmarks.results.reporting.agentx import agentx_sinks
from benchmarks.results.types import BenchmarkRun


def run_agentx(
    config: AgentXConfig, service: ModelService, *, output_dir: str | None = None,
    label: str = "", wandb_group: str = "",
) -> BenchmarkRun:
    """Run one native replay, save its measurements, and release its child process before service cleanup."""
    record = {"mode": "agentx", "model": service.model, "harness_version": HARNESS_VERSION}
    with ResultOutputs(
        config, service, directory_prefix="agentx-", output_dir=output_dir,
        sink_factory=lambda directory: agentx_sinks(config, record, directory, label=label, group=wandb_group),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir)
        native = directory / "native"
        native.mkdir()
        if config.service.health_url:
            asyncio.run(require_health_endpoint(config.service.health_url))
        arguments, environment = profile_command(config, service, native)
        logger.info("AgentX {} · model={} · native results={}", HARNESS_VERSION, service.model, native)
        with (
            (directory / "agentx.log").open("w", encoding="utf-8") as log,
            (CommandExecution.current() or CommandExecution()) as execution,
            execution.process(
                arguments, env=environment, interrupt_signal=signal.SIGINT, shutdown_grace=15,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            ) as process,
        ):
            outputs.start_measurement()
            for line in process.stdout:
                execution.check()
                if service.api_key and service.api_key != "EMPTY":
                    line = line.replace(service.api_key, "[redacted]")
                log.write(line)
                log.flush()
                print(line, end="", file=sys.stderr, flush=True)
            code = process.wait()
            execution.check()
        measured = read_metrics(native, gpu_count=service.gpu_count, concurrency=config.max_concurrency)
        state = measured["agentx"]
        if state["was_cancelled"]:
            raise KeyboardInterrupt("AgentX profiling was cancelled; native reports remain available")
        if not state["summary_available"]:
            raise AgentXExecutionError(
                f"AgentX exited {code} without a completed profiling summary "
                f"({measured['request_num']} observed requests); see {directory}"
            )
        run = BenchmarkRun(
            record, measured, None, {"native": native, "agentx_log": directory / "agentx.log"},
            exit_code=code,
        )
        outputs.publish(run)
        if code:
            logger.error("AgentX exited with {}; native reports and logs retained in {}", code, directory)
        return run
