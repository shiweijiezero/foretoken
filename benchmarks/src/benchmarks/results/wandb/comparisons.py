# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish paired distribution and generated-sequence comparisons to W&B."""

from __future__ import annotations

from typing import Any

import wandb

from benchmarks.results.recording.comparisons import (
    _GREEDY_CANDIDATE_COLUMNS,
    _GREEDY_SAMPLE_COLUMNS,
    _columns,
)
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.evaluation import publish_quality_wandb


def publish_distribution_comparison_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish native quality scores, candidate tables, and position-indexed curves."""
    publish_quality_wandb(sdk_run, run)
    comparison = run.metrics["distribution_comparison"]
    protocol = comparison["protocol"]
    points = comparison["candidates"]
    positions = comparison["positions"]
    point_columns, position_columns = _columns(protocol)
    sdk_run.log({
        "Distribution Comparison/Candidates": wandb.Table(
            columns=list(point_columns),
            data=[[row[column] for column in point_columns] for row in points],
            allow_mixed_types=True,
        ),
    })
    if positions:
        sdk_run.log({
            "Distribution Comparison/Positions": wandb.Table(
                columns=list(position_columns),
                data=[[row[column] for column in position_columns] for row in positions],
                allow_mixed_types=True,
            ),
        })
        for field, title in (
            ("kl", "KL by scored position index (nats)"),
            ("centered_logit_rmse", "Centered-logit RMSE by scored position index"),
        ):
            series = []
            for point in points:
                label = point["label"]
                rows = [row for row in positions if row["candidate"] == label and row[field] is not None]
                if rows:
                    series.append((label, [float(row["scored_index"]) for row in rows], [float(row[field]) for row in rows]))
            if series:
                sdk_run.log({
                    f"Distribution Comparison/{title}": wandb.plot.line_series(
                        xs=[item[1] for item in series],
                        ys=[item[2] for item in series],
                        keys=[item[0] for item in series],
                        xname="Scored position index (not time)",
                        title=title,
                    ),
                })


def publish_greedy_comparison_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish candidate scores and native generated-sequence records through quality W&B."""
    publish_quality_wandb(sdk_run, run)
    comparison = run.metrics["greedy_comparison"]
    for name, columns, rows in (
        ("Candidates", _GREEDY_CANDIDATE_COLUMNS, comparison["candidates"]),
        ("Samples", _GREEDY_SAMPLE_COLUMNS, comparison["samples"]),
    ):
        sdk_run.log({f"Greedy Comparison/{name}": wandb.Table(
            columns=list(columns),
            data=[[row[key] for key in columns] for row in rows],
            allow_mixed_types=True,
        )})
    scored = [row for row in comparison["candidates"] if row["exact_match_rate"] is not None]
    if scored:
        table = wandb.Table(
            columns=["Candidate", "Exact sequence match (%)"],
            data=[[row["label"], row["exact_match_rate"] * 100] for row in scored],
        )
        sdk_run.log({"Greedy Comparison/Exact sequence match": wandb.plot.bar(
            table, "Candidate", "Exact sequence match (%)", title="Greedy generated-sequence agreement",
        )})
    series = [
        (point["label"], [row for row in comparison["samples"]
                          if row["candidate"] == point["label"] and row["first_divergence"] is not None])
        for point in comparison["candidates"]
    ]
    series = [(label, rows) for label, rows in series if rows]
    if series:
        sdk_run.log({"Greedy Comparison/First divergence": wandb.plot.line_series(
            xs=[[row["window"] for row in rows] for _, rows in series],
            ys=[[row["first_divergence"] for row in rows] for _, rows in series],
            keys=[label for label, _ in series], xname="Sample index",
            title="First differing generated token position",
        )})
