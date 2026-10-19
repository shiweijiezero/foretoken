# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Summarize recorded sweep repetitions and export their existing scalar evidence."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any


def summarize_sweep(
    points: list[dict[str, Any]], *, num_runs: int | None = None
) -> list[dict[str, Any]]:
    """Summarize each measured scalar across repetitions, keeping conditions and missing samples."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        method = str(point.get("bench", {}).get("service", {}).get("name", ""))
        groups[method, str(point["combination"])].append(point)
    rows: list[dict[str, Any]] = []
    for (method, combination), runs in groups.items():
        samples: dict[str, list[float | int]] = defaultdict(list)
        for run in runs:
            for metric, value in _scalar_metrics(run).items():
                samples.setdefault(metric, [])
                if value is not None:
                    samples[metric].append(value)
        identity = {
            "combination": combination,
            "parameter_group": runs[0]["parameter_group"],
            "method": method,
            "bench": runs[0]["bench"],
            "runs": sum(run.get("status") != "pending" for run in runs),
            "requested_runs": num_runs if num_runs is not None else len(runs),
            "failed_runs": sum(
                bool(run.get("exit_code")) or bool(run.get("failed_num", 0))
                for run in runs
            ),
        }
        for metric, values in samples.items():
            numeric = [float(value) for value in values]
            rows.append(
                {
                    **identity,
                    "metric": metric,
                    "samples": len(numeric),
                    "mean": mean(numeric) if numeric else None,
                    "median": median(numeric) if numeric else None,
                    "stddev": stdev(numeric) if len(numeric) > 1 else None,
                    "min": min(numeric) if numeric else None,
                    "max": max(numeric) if numeric else None,
                }
            )
        if not samples:
            rows.append(
                {
                    **identity,
                    "metric": "",
                    "samples": 0,
                    "mean": None,
                    "median": None,
                    "stddev": None,
                    "min": None,
                    "max": None,
                }
            )
    return rows


def write_sweep_csv(rows: list[dict[str, Any]], directory: str) -> Path:
    """Write repeat statistics and experimental conditions for spreadsheet consumers."""
    path = Path(directory) / "sweep_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "combination",
                "parameter_group",
                "method",
                "bench",
                "runs",
                "requested_runs",
                "failed_runs",
                "metric",
                "samples",
                "mean",
                "median",
                "stddev",
                "min",
                "max",
            ),
        )
        writer.writeheader()
        writer.writerows(
            {**row, "bench": json.dumps(row["bench"], ensure_ascii=False)}
            for row in rows
        )
    return path


def _scalar_metrics(value: Any, prefix: str = "") -> dict[str, float | int | None]:
    """Flatten numeric leaves, retaining missing measurements as absent samples."""
    if isinstance(value, bool):
        return {}
    if value is None or isinstance(value, (int, float)):
        measured = value if value is not None and math.isfinite(value) else None
        return {prefix.rstrip("_"): measured} if prefix else {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, float | int | None] = {}
    for key, child in value.items():
        if key in {
            "combination",
            "parameter_group",
            "bench",
            "label",
            "run_number",
            "exit_code",
        }:
            continue
        child_prefix = f"{prefix}{key}_"
        if prefix == "throughput_":
            child_prefix = f"{key}_"
        if prefix in {"latency_", "ttft_", "tpot_", "itl_"} and key in {
            "mean",
            "p50",
            "p95",
            "p99",
        }:
            child_prefix = f"{prefix}{key}_seconds_"
        result.update(_scalar_metrics(child, child_prefix))
    return result
