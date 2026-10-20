# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Materialize paired candidate and per-position comparison evidence."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

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


_GREEDY_CANDIDATE_COLUMNS = (
    "model", "label", "method", "weight_bits", "bits_per_weight", "model_size_gib",
    "valid_samples", "failed_samples", "exact_matches", "exact_match_rate",
)


_GREEDY_SAMPLE_COLUMNS = (
    "candidate", "window", "reference_token_ids", "candidate_token_ids",
    "reference_text", "candidate_text", "reference_finish_reason", "candidate_finish_reason",
    "reference_length", "candidate_length", "error", "exact_sequence_match", "first_divergence", "mismatch_count",
)


def write_greedy_comparison_artifacts(directory: Path, metrics: dict[str, Any]) -> dict[str, Path]:
    """Retain paired generated-sequence records and candidate statistics as stable CSV projections."""
    comparison = metrics["greedy_comparison"]
    artifacts = {}
    for name, columns, rows in (
        ("candidates", _GREEDY_CANDIDATE_COLUMNS, comparison["candidates"]),
        ("samples", _GREEDY_SAMPLE_COLUMNS, comparison["samples"]),
    ):
        path = directory / f"greedy_comparison_{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row[key], list) else row[key] for key in columns})
        artifacts[f"greedy_comparison_{name}"] = path
    return artifacts


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
