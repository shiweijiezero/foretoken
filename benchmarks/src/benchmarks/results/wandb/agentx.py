# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish native AgentX results through the shared W&B recording lifecycle."""

from __future__ import annotations

from typing import Any

from benchmarks.results.measurements.sweeps import _scalar_metrics
from benchmarks.results.types import BenchmarkRun


def publish_agentx_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish measured scalars and native submission status without fabricating HTTP histories."""
    session.summary.update({f"AgentX/{name}": value for name, value in _scalar_metrics(run.metrics).items()
                            if value is not None})
    native = run.metrics["agentx"]
    session.summary.update({
        "AgentX/submission_valid": native["submission_valid"],
        "AgentX/submission_invalid_reasons": native["submission_invalid_reasons"],
        "AgentX/was_cancelled": native["was_cancelled"],
        "AgentX/interactivity_source": native["interactivity_source"],
    })
