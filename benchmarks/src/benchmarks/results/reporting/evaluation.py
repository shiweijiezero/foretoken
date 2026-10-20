# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present native quality scores without recomputing or merging benchmark metrics."""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.results.recording.files import write_json
from benchmarks.results.reporting.local import LocalDirectorySink
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.evaluation import (
    publish_evaluation_comparison_wandb,
    publish_quality_wandb,
)
from benchmarks.results.wandb.recording import WandbSink

logger = logging.getLogger(__name__)


def evaluation_sinks(
    config: EvaluationConfig, record: dict[str, Any], directory: str,
    *,
    console_sink: ResultSink | None = None,
    publisher: Callable[[Any, BenchmarkRun], None] = publish_quality_wandb,
) -> list[ResultSink]:
    """Use the existing publication lifecycle with evaluation-specific presentation."""
    sinks: list[ResultSink] = [EvaluationArtifactSink(config, directory)]
    sinks.append(console_sink if console_sink is not None else EvaluationConsoleSink())
    if config.outputs.saves_local:
        sinks.append(LocalDirectorySink(directory))
    if config.outputs.includes("wandb"):
        sinks.append(
            WandbSink(
                config,
                execution_dir=directory,
                run_name=config.wandb.run_name
                or "_".join(str(part) for part in (
                    record.get("model"), record.get("evaluator"), Path(directory).name,
                ) if part),
                group=config.wandb.group,
                publisher=publisher,
                run_config={**config.to_dict(), **record},
            )
        )
    return sinks


def evaluation_comparison_sinks(config: EvaluationConfig, record: dict[str, Any], directory: str) -> list[ResultSink]:
    """Compose one quality comparison around the existing result destinations."""
    sinks = evaluation_sinks(
        config, record, directory,
        console_sink=EvaluationComparisonConsoleSink(),
        publisher=publish_evaluation_comparison_wandb,
    )
    return [sinks[0], EvaluationComparisonArtifactSink(directory), *sinks[1:]]


def _display(row: dict[str, Any]) -> str:
    """Use upstream display units when provided, otherwise show the original value."""
    value = row["value"]
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return f"{value * row['display_multiplier']:.4g}{row['display_unit']}"
    return "—" if value is None else str(value)


def _execution_lines(execution: dict[str, Any]) -> list[str]:
    """Format native task completion counts for single evaluations and method comparisons."""
    return [
        f"{task}: {status.get('succeeded')} / {status.get('requested')} samples scored; {status.get('errored')} errors; incomplete={status.get('incomplete')}"
        for task, status in execution.items()
    ]


class EvaluationConsoleSink:
    """Print a readable task summary while keeping all detailed metrics in artifacts."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Print all task/group metrics with native filters and standard errors."""
        rows = run.metrics["scores"]
        visible = [row for row in rows if row["level"] in ("task", "group")]
        table = [["Task", "Metric", "Filter", "Samples", "Value", "Std. error"]]
        for row in visible:
            table.append(
                [
                    row["task"],
                    row["metric"],
                    row["filter"] or "—",
                    str(row["samples"]) if row["samples"] is not None else "—",
                    _display(row),
                    str(row["stderr"]) if row["stderr"] is not None else "—",
                ]
            )
        widths = [max(len(line[i]) for line in table) for i in range(len(table[0]))]
        status = "failed" if run.exit_code else "completed"
        if not run.exit_code and any(
            item.get("incomplete") for item in run.metrics["execution"].values()
        ):
            status = "completed with incomplete results"
        lines = [
            "Foretoken evaluation",
            f"Model: {run.record['model']}    Evaluator: {run.record['evaluator']}",
            f"Status: {status}    Duration: {run.metrics['duration_seconds']:.1f}s",
            "",
        ]
        for line in table:
            lines.append(
                "  ".join(value.ljust(width) for value, width in zip(line, widths))
            )
        if not visible:
            lines.append("No scored tasks. See evaluator.log and native outputs.")
        if any(row["level"] in ("category", "subset") for row in rows):
            lines.append(
                "Category and subset scores are included in metrics.json and W&B Scores."
            )
        lines.extend(_execution_lines(run.metrics["execution"]))
        logger.info("\n%s", "\n".join(lines))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationArtifactSink:
    """Save evaluation invocation identity beside native artifacts and shared metrics."""

    def __init__(self, config: EvaluationConfig, directory: str) -> None:
        self.config = config
        self.directory = directory

    def open(self, record: dict[str, Any]) -> None:
        write_json(self.directory, "config.json", {**self.config.to_dict(), **record})

    def publish(self, run: BenchmarkRun) -> None:
        run.artifacts["config"] = Path(self.directory) / "config.json"

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationComparisonArtifactSink:
    """Save aligned multi-method scores as a spreadsheet beside child evaluator outputs."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Write aligned native task scores with their filters, units, and uncertainty for spreadsheet consumers."""
        columns = ("method", "task", "level", "subset", "filter", "metric", "value",
                   "stderr", "samples", "direction", "display_multiplier", "display_unit", "primary")
        path = self.directory / "evaluation_comparison.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(run.metrics["scores"])
        run.artifacts["evaluation_comparison_csv"] = path

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationComparisonConsoleSink:
    """Show aligned task scores for each evaluated deployment."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Show each method outcome and task score without pooling samples across methods."""
        for method in run.metrics["evaluation_comparison"]["methods"]:
            native = run.metrics["execution"]["methods"].get(method["label"], {})
            status = method["status"]
            if status == "completed" and any(task.get("incomplete") for task in native.values()):
                status = "completed with incomplete results"
            logger.info("Method %s (%s): %s", method["label"], method["model"], status)
            for line in _execution_lines(native):
                logger.info("  %s", line)
        for row in run.metrics["scores"]:
            if row["level"] == "task":
                logger.info("%s · %s · %s/%s: %s", row["method"], row["task"],
                            row["metric"], row["filter"], _display(row))

    def close(self, *, exit_code: int = 0) -> None:
        return None
