# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present and publish paired next-token distribution comparisons."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

import wandb

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.results.evaluation import evaluation_sinks, publish_quality_wandb
from benchmarks.results.output import BenchmarkRun, ResultSink

logger = logging.getLogger(__name__)

_POINT_COLUMNS = (
    "model",
    "label",
    "method",
    "weight_bits",
    "bits_per_weight",
    "model_size_gib",
    "scored_positions",
    "vocab_size",
    "mean_kl",
    "median_kl",
    "p99_kl",
    "top1_agreement",
    "corpus_token_mean_delta_p",
    "corpus_token_rms_delta_p",
    "mean_centered_logit_rmse",
    "mean_total_variation",
)
_POSITION_COLUMNS = (
    "candidate",
    "window",
    "position",
    "scored_index",
    "kl",
    "top1_match",
    "corpus_token_delta_p",
    "centered_logit_rmse",
    "total_variation",
)


def _columns(protocol: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Add exactly the top-k overlap columns requested by the scoring protocol."""
    top_k = tuple(protocol["top_k"])
    point_columns = _POINT_COLUMNS + tuple(f"top{k}_overlap" for k in top_k)
    position_columns = _POSITION_COLUMNS + tuple(f"top{k}_overlap" for k in top_k)
    return point_columns, position_columns


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write a stable tabular projection for local inspection and downstream import."""
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row[column] for column in columns})


def write_distribution_comparison_artifacts(directory: str | Path, metrics: dict[str, Any]) -> dict[str, Path]:
    """Save candidate statistics and scored positions for all result destinations."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    comparison = metrics["distribution_comparison"]
    protocol = comparison["protocol"]
    points = comparison["candidates"]
    positions = comparison["positions"]
    point_columns, _ = _columns(protocol)
    candidate_path = out / "distribution_comparison_candidates.csv"
    positions_path = out / "distribution_comparison_positions.jsonl"
    _write_csv(candidate_path, point_columns, points)
    with positions_path.open("w", encoding="utf-8") as stream:
        for row in positions:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    artifacts = {"distribution_comparison_candidates": candidate_path, "distribution_comparison_positions": positions_path}
    return artifacts


class DistributionComparisonArtifactSink:
    """Materialize comparison records before downstream publishers consume artifacts."""

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        run.artifacts.update(write_distribution_comparison_artifacts(self.directory, run.metrics))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class DistributionComparisonConsoleSink:
    """Print one compact candidate table and protocol summary for a distribution comparison run."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        comparison = run.metrics["distribution_comparison"]
        protocol = comparison["protocol"]
        points = comparison["candidates"]
        expected = protocol["num_windows"] * protocol["score_tokens"]
        execution = run.metrics["execution"]["distribution_comparison"]
        lines = [
            "Foretoken model distribution comparison",
            f"Reference: {comparison['reference_model']}    Scored positions/candidate: {expected}    Completed: {execution['succeeded']}/{execution['requested']}",
            "Protocol: " + ", ".join(
                f"{key}={protocol[key]}"
                for key in ("context_length", "num_windows", "score_tokens", "top_k")
            ),
            "",
        ]
        headers = ["Candidate", "Method", "Bits", "Mean KL (nats)", "p99 KL (nats)", "Top-1 (%)", "Centered RMSE"]
        table = [headers]
        for point in points:
            table.append([
                point["label"],
                point["method"],
                _bits_label(point),
                _format(point["mean_kl"]),
                _format(point["p99_kl"]),
                _format(point["top1_agreement"] * 100),
                _format(point["mean_centered_logit_rmse"]),
            ])
        widths = [max(len(str(row[index])) for row in table) for index in range(len(headers))]
        lines.extend("  ".join(str(value).ljust(width) for value, width in zip(row, widths)) for row in table)
        logger.info("\n%s", "\n".join(lines))

    def close(self, *, exit_code: int = 0) -> None:
        return None


def _format(value: Any) -> str:
    """Format a scalar for compact console output."""
    return "—" if value is None else f"{float(value):.4g}"


def _bits_label(point: dict[str, Any]) -> str:
    """Show nominal precision and measured effective bits without conflating them."""
    nominal = point["weight_bits"]
    effective = point["bits_per_weight"]
    if nominal is None and effective is None:
        return "—"
    if nominal is None:
        return f"eff {effective:g}"
    if effective is None:
        return f"nom {nominal:g}"
    return f"{nominal:g}/{effective:g}"


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


def distribution_comparison_sinks(config: EvaluationConfig, record: dict[str, Any], directory: str) -> list[ResultSink]:
    """Compose evaluation publication with distribution comparison artifacts and presentation."""
    sinks = evaluation_sinks(
        config,
        record,
        directory,
        console_sink=DistributionComparisonConsoleSink(),
        publisher=publish_distribution_comparison_wandb,
    )
    return [sinks[0], DistributionComparisonArtifactSink(directory), *sinks[1:]]
