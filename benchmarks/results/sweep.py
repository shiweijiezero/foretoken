# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Summarize repeated workload points without pooling unrelated request distributions."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any


def summarize_sweep(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return long-form rows for JSON/CSV comparisons of each point across repetitions.

    Every repetition contributes, including failed runs. Missing timing values
    are counted separately; a summary of run percentiles is not a pooled percentile.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        groups[point["combination"]].append(point)
    rows = []
    for combination, runs in groups.items():
        samples: dict[str, list[float | None]] = defaultdict(list)
        for run in runs:
            for metric in ("request_num", "success_num", "failed_num", "success_rate",
                           "avg_input_tokens", "avg_output_tokens", "benchmark_time"):
                samples[metric].append(run[metric])
            for metric in ("requests_per_second", "generation_tokens_per_second"):
                samples[metric].append(run["throughput"][metric])
            for metric in ("latency", "ttft", "tpot", "itl"):
                for statistic in ("mean", "p50", "p95", "p99"):
                    samples[f"{metric}_{statistic}_seconds"].append(run[metric][statistic])
        for metric, values in samples.items():
            available = [value for value in values if value is not None]
            rows.append({
                "combination": combination,
                "parameter_group": runs[0]["parameter_group"],
                "metric": metric,
                "runs": len(runs),
                "samples": len(available),
                "mean": mean(available) if available else None,
                "median": median(available) if available else None,
                "stddev": stdev(available) if len(available) > 1 else None,
                "min": min(available) if available else None,
                "max": max(available) if available else None,
            })
    return rows


def write_sweep_csv(rows: list[dict[str, Any]], directory: str) -> Path:
    """Write the same explicit summary fields as JSON for spreadsheet and plotting tools."""
    path = Path(directory) / "sweep_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=(
            "combination", "parameter_group", "metric", "runs", "samples",
            "mean", "median", "stddev", "min", "max",
        ))
        writer.writeheader()
        writer.writerows(rows)
    return path
