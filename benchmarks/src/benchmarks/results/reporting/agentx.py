# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present official AgentX measurements and keep submission validity visible."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from tabulate import tabulate

from benchmarks.config.agentx import AgentXConfig
from benchmarks.results.reporting.local import LocalDirectorySink
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.agentx import publish_agentx_wandb
from benchmarks.results.wandb.recording import WandbSink


def agentx_sinks(
    config: AgentXConfig, record: dict[str, Any], directory: str, *, label: str, group: str,
) -> list[ResultSink]:
    """Compose native presentation with the common local and W&B destinations."""
    sinks: list[ResultSink] = [AgentXConsoleSink()]
    if config.outputs.saves_local:
        sinks.append(LocalDirectorySink(directory))
    if config.outputs.includes("wandb"):
        sinks.append(WandbSink(
            config, execution_dir=directory,
            run_name=config.wandb.run_name or label or f"agentx-{Path(directory).name}",
            group=group or config.wandb.group, publisher=publish_agentx_wandb,
            run_config={**config.to_dict(), **record},
        ))
    return sinks


class AgentXConsoleSink:
    """Show native execution outcomes and the measurements available for comparison."""

    def open(self, record: dict[str, Any]) -> None:
        """Acquire no resources before publishing the native summary."""
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Print observed counts, official interactivity, and the submission-validity outcome."""
        metrics, native = run.metrics, run.metrics["agentx"]
        rows = [
            ("Completed requests", metrics["request_num"]),
            ("Successful requests", metrics["success_num"]),
            ("Failed requests", metrics["failed_num"]),
            ("Total output tokens", metrics["total_output_tokens"]),
            ("Measured duration (s)", metrics["benchmark_time"]),
            ("Output throughput (tokens/s)", metrics["throughput"]["generation_tokens_per_second"]),
            ("Total throughput (tokens/s)", metrics["throughput"]["total_tokens_per_second"]),
            ("AgentX interactivity P90 (tokens/s/user)", metrics.get("agentx_interactivity_p90")),
        ]
        rendered = [(name, "unavailable" if value is None else f"{value:,}" if isinstance(value, int) else f"{value:,.4g}")
                    for name, value in rows]
        status = "failed" if run.exit_code else "completed"
        logger.info("AgentX {} · model={}\n{}", status, run.record["model"],
                    tabulate(rendered, tablefmt="plain"))
        logger.info("AgentX submission valid: {}{}", native["submission_valid"],
                    f" · {', '.join(native['submission_invalid_reasons'])}" if native["submission_invalid_reasons"] else "")

    def close(self, *, exit_code: int = 0) -> None:
        """Release no resources after console publication."""
        return None
