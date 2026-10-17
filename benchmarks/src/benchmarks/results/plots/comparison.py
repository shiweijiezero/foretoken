# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compare saved performance summaries against an explicitly selected approach."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.results.plots.saved_results import SavedResults, _comparison_changes


def compare_to_base(
    results: SavedResults, base_directory: Path, *, metrics: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Build base-relative CSV rows for the saved-result exporter without rerunning measurements.

    Workload identities and repeat statistics come from the existing reader. Each
    metric compares run means, not paired requests; missing workloads remain separate
    rows. Environment differences are evidence for review, not a comparability verdict.
    """
    summaries: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for row in results.summary:
        # Native evaluations may have summary placeholders when they fail before scoring.
        if row["parameter_group"] in {
            "evaluation", "evaluation_comparison", "distribution_comparison", "greedy_comparison", "slo_search",
        }:
            continue
        summaries.setdefault(row["method"], {}).setdefault(row["combination"], {})[row["metric"]] = row
    bases = [name for name in results.base_approaches if name in summaries]
    if not bases:
        raise ValueError(f"No saved performance results in base directory: {base_directory}")
    if len(bases) != 1:
        raise ValueError(f"Base directory contains multiple approaches: {', '.join(bases)}. Select one result or iteration directory")
    base = bases[0]
    if len(summaries) < 2:
        raise ValueError("Base comparison requires another performance approach; select candidate results as well")

    evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in results.runs:
        if "approach" in run:
            evidence[run["approach"], run["workload"]].append(run)

    # Keep actual workload and completion outcomes alongside selected speed metrics.
    contextual_metrics = {
        "planned_num", "admitted_num", "not_executed_num",
        "request_num", "success_num", "failed_num", "success_rate",
        "avg_input_tokens", "avg_output_tokens", "avg_cached_input_tokens",
    }
    selected = set(metrics) | contextual_metrics if metrics else None
    rows = []
    for candidate, workloads in summaries.items():
        if candidate == base:
            continue
        for workload in dict.fromkeys((*summaries[base], *workloads)):
            base_metrics = summaries[base].get(workload, {})
            candidate_metrics = workloads.get(workload, {})
            base_runs = evidence[base, workload]
            candidate_runs = evidence[candidate, workload]
            changes = [{**run} for run in (*base_runs, *candidate_runs)]
            _comparison_changes(changes)
            settings = {
                "conditions": (base_runs or candidate_runs)[0]["conditions"],
                "condition_differences": [
                    {"approach": run["approach"], "result": run["result"],
                     "workload": run["conditions_changes"], "environment": run["environment_changes"]}
                    for run in changes
                    if run["conditions_changes"] or run["environment_changes"]
                ],
                "missing_environments": [run["result"] for run in changes if run["environment_settings"] is None],
                "base_results": [run["result"] for run in base_runs],
                "candidate_results": [run["result"] for run in candidate_runs],
                "base_environments": [run["environment"] for run in base_runs if run["environment"]],
                "candidate_environments": [run["environment"] for run in candidate_runs if run["environment"]],
            }
            metric_names = list(dict.fromkeys((*base_metrics, *candidate_metrics)))
            if selected is not None:
                metric_names = [name for name in metric_names if name in selected]
            # Failed attempts without metrics must remain visible even with --metric.
            if not metric_names:
                metric_names = [""]
            for metric in metric_names:
                reference = base_metrics.get(metric)
                measured = candidate_metrics.get(metric)
                base_value = reference["mean"] if reference else None
                candidate_value = measured["mean"] if measured else None
                if not base_metrics:
                    status = "missing_base_workload"
                elif not candidate_metrics:
                    status = "missing_candidate_workload"
                elif base_value is None or candidate_value is None:
                    status = "missing_metric"
                elif base_value == 0:
                    status = "zero_base"
                else:
                    status = "compared"
                row = {
                    "workload": workload, "metric": metric, "base": base, "candidate": candidate,
                    "base_value": base_value, "candidate_value": candidate_value,
                    "change_percent": (candidate_value / base_value - 1) * 100 if status == "compared" else None,
                    "status": status,
                }
                for side, values, group in (
                    ("base", reference, base_metrics), ("candidate", measured, candidate_metrics),
                ):
                    row[f"{side}_samples"] = values["samples"] if values else 0
                    row[f"{side}_stddev"] = values["stddev"] if values else None
                    # Attempt counts belong to the workload, even when this metric is absent.
                    attempts = next(iter(group.values()), None)
                    for field in ("runs", "requested_runs", "failed_runs"):
                        row[f"{side}_{field}"] = attempts[field] if attempts else 0
                rows.append({**row, **settings})
    return rows
