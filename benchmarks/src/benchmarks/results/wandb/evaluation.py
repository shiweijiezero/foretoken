# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish recorded native quality scores and comparison charts to W&B."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import wandb

from benchmarks.results.types import BenchmarkRun

logger = logging.getLogger(__name__)


def publish_quality_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish native-valued scalars and a complete score table to the final online session."""
    columns = [
        *(["method"] if run.record.get("mode") == "evaluation_comparison" else []),
        "task",
        "level",
        "subset",
        "filter",
        "metric",
        "value",
        "stderr",
        "samples",
        "direction",
        "primary",
    ]
    rows = run.metrics["scores"]
    session.log(
        {
            "Evaluation/Scores": wandb.Table(
                columns=columns,
                data=[[row[key] for key in columns] for row in rows],
                allow_mixed_types=True,
            )
        }
    )
    summary = {
        "Evaluation/Exit code": run.exit_code,
        "Evaluation/Duration (s)": run.metrics["duration_seconds"],
    }
    for row in rows:
        if isinstance(row["value"], (int, float)) and not isinstance(
            row["value"], bool
        ):
            key = "/".join(
                quote(str(row[k]), safe="") or "all"
                for k in (("method",) if "method" in row else ()) + ("task", "level", "subset", "filter", "metric")
            )
            summary[f"Evaluation/{key}"] = row["value"]
    summary["Evaluation/Execution"] = run.metrics["execution"]
    session.summary.update(summary)
    if session.url:
        logger.info("W&B results: %s", session.url)


def publish_evaluation_comparison_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish matched task scores and child artifacts through the quality W&B sink."""
    publish_quality_wandb(session, run)
    from benchmarks.results.plots.charts.quality import _evaluation_comparison_charts

    for chart in _evaluation_comparison_charts(run.metrics["evaluation_comparison"]):
        table = wandb.Table(columns=["Method", chart.ylabel], data=[
            [series.name, series.y[0]] for series in chart.series
        ])
        session.log({f"Evaluation Comparison/{chart.name}": wandb.plot.bar(
            table, "Method", chart.ylabel, title=chart.title,
        )})
