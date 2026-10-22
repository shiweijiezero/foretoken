# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present and publish paired next-token distribution comparisons."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from tabulate import tabulate

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.results.recording.comparisons import (
    write_distribution_comparison_artifacts,
    write_greedy_comparison_artifacts,
)
from benchmarks.results.reporting.evaluation import evaluation_sinks
from benchmarks.results.types import BenchmarkRun, ResultSink
from benchmarks.results.wandb.comparisons import (
    publish_distribution_comparison_wandb,
    publish_greedy_comparison_wandb,
)


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


def greedy_comparison_sinks(config: EvaluationConfig, record: dict[str, Any], directory: str) -> list[ResultSink]:
    """Compose native-sequence artifacts with the existing evaluation publishers."""
    sinks = evaluation_sinks(
        config, record, directory,
        console_sink=GreedyComparisonConsoleSink(),
        publisher=publish_greedy_comparison_wandb,
    )
    return [sinks[0], GreedyComparisonArtifactSink(directory), *sinks[1:]]


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
        """Present paired candidate statistics and completed scoring positions without rescoring them."""
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
        lines.append(tabulate(
            table[1:], headers=headers, tablefmt="plain", disable_numparse=True,
            colalign=("left", "left", "right", "right", "right", "right", "right"),
        ))
        logger.info("\n{}", "\n".join(lines))

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


class GreedyComparisonArtifactSink:
    """Write candidate and per-sample CSV projections before local and W&B publication."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Retain candidate statistics and native generated sequences as result-owned CSV artifacts."""
        run.artifacts.update(write_greedy_comparison_artifacts(self.directory, run.metrics))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class GreedyComparisonConsoleSink:
    """Show exact-match rates and failed sample counts without pooling failed requests."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Report exact sequence matches separately from invalid or failed samples."""
        comparison = run.metrics["greedy_comparison"]
        logger.info("Greedy generation comparison · reference {}", comparison["reference_model"])
        for point in comparison["candidates"]:
            rate = point["exact_match_rate"]
            logger.info(
                "{}: exact {}, valid {}, failed {}",
                point["label"], f"{rate:.1%}" if rate is not None else "—",
                point["valid_samples"], point["failed_samples"],
            )

    def close(self, *, exit_code: int = 0) -> None:
        return None
