# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project sampled Prometheus and GPU allocation evidence without estimating new measurements."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.results.measurements.timeseries import ELAPSED_TIME
from benchmarks.results.observations.replicas import gpu_allocation_history_rows
from benchmarks.results.plots.data import Chart, Series, _numeric


def _prometheus_charts(source: Path) -> list[Chart]:
    """Plot observer series on their elapsed clock, retaining complete labels in CSV records."""
    path = source / "prometheus_observations.json"
    if not path.is_file():
        return []
    observations = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in observations["samples"]:
        elapsed = sample.get("elapsed_time_s")
        if elapsed is None:
            continue
        for name, query in sample["queries"].items():
            for result in query["result"]:
                labels = json.dumps(result.get("metric") or {}, sort_keys=True)
                value = result.get("value")
                numeric = (
                    _numeric(float(value[1]))
                    if value and value[1] is not None
                    else None
                )
                if numeric is not None:
                    grouped[(name, labels)].append(
                        {
                            "elapsed_time_s": elapsed,
                            "value": numeric,
                            "query": name,
                            "labels": labels,
                            "expression": query["expression"],
                            "observer_errors": observations["errors"],
                        }
                    )
    charts = []
    for name in dict.fromkeys(key[0] for key in grouped):
        def display_label(label: str, *, name: str = name) -> str:
            """Show service, group, role and rank without printing raw JSON in figure titles."""
            values = json.loads(label)
            if name.startswith(("cpu_", "memory_")):
                same_pod = sum(
                    1 for metric, other in grouped
                    if metric == name and json.loads(other).get("pod") == values.get("pod")
                )
                return values["pod"] + ("\n" + values["id"].rsplit("/", 1)[-1] if same_pod > 1 else "")
            if not name.startswith("spec_"):
                identity = ("pod", "device_id") if values.get("pod") else (
                    "model_name", "model_group", "model_role", "route_target_id", "data_parallel_rank",
                )
                return "\n".join(str(values[key]) for key in identity if values.get(key) is not None) or label
            group = values.get("model_group") or values.get("modelservice") or values.get("model_name")
            role = values.get("model_role") or values.get("inference_foretoken_io_model_role")
            return " / ".join(str(item) for item in (group, role, values.get("engine")) if item is not None)

        series = tuple(
            Series(
                display_label(label),
                tuple(row["elapsed_time_s"] for row in grouped[(name, label)]),
                tuple(row["value"] for row in grouped[(name, label)]),
                (None,) * len(grouped[(name, label)]),
                tuple(grouped[(name, label)]),
            )
            for metric, label in grouped
            if metric == name
        )
        if series:
            unit = (
                "cores"
                if name.endswith("_cores")
                else "bytes"
                if name.endswith("_bytes")
                else "µs"
                if name.endswith("_microseconds")
                else "GPU s/s"
                if name in {"spec_draft_gpu_seconds_per_second", "spec_target_forward_gpu_seconds_per_second"}
                else "tokens/draft"
                if name == "spec_accepted_tokens_per_draft"
                else "ratio"
                if name.endswith("_ratio")
                else "W"
                if name.endswith("_watts")
                else "°C"
                if name.endswith("_celsius")
                else "tokens/s"
                if name.endswith("_per_second")
                else "selections/s"
                if name.endswith("_rate")
                else "requests"
            )
            label = {
                "cpu_usage_cores": "CPU usage",
                "cpu_quota_cores": "CPU quota",
                "cpu_quota_microseconds": "CPU quota per period",
                "cpu_throttled_periods_ratio": "Throttled CPU periods",
                "memory_working_set_bytes": "Container memory working set",
                "spec_draft_gpu_seconds_per_second": "Draft GPU work",
                "spec_target_forward_gpu_seconds_per_second": "Target forward GPU work",
                "spec_draft_time_share_ratio": "Draft GPU-time share",
                "spec_target_forward_time_share_ratio": "Target forward GPU-time share",
                "spec_acceptance_ratio": "Draft acceptance",
                "spec_accepted_tokens_per_draft": "Accepted tokens per draft",
            }.get(name, f"Prometheus · {name.replace('_', ' ')}")
            title = f"{label} · 1-minute rate" if name in {"cpu_usage_cores", "cpu_throttled_periods_ratio"} else label
            charts.append(
                Chart(
                    f"prometheus-{name}",
                    title,
                    "Elapsed time (s)",
                    f"{label} ({unit})",
                    series,
                    metric=name,
                )
            )
    return charts


def gpu_allocation_charts(source: Path) -> list[Chart]:
    """Plot sampled allocated GPU counts by resource on the benchmark clock.

    The observer owns coverage and integration. Saved history rows retain the
    boundary samples and unknown intervals, so the chart never estimates cost.
    """
    path = source / "gpu_allocation.json"
    if not path.is_file():
        return []
    allocation = json.loads(path.read_text(encoding="utf-8"))
    rows = gpu_allocation_history_rows(allocation)
    resources = sorted({key for row in rows for key in row if key.startswith("Resources/")})
    charts = []
    for field in resources:
        resource = field.removeprefix("Resources/").removesuffix("/Allocated GPUs")
        records = tuple({**row, "resource_name": resource, "duration_s": allocation["duration_s"]} for row in rows)
        values = tuple(
            float(row[field]) if _numeric(row.get(field)) is not None else math.nan
            for row in rows
        )
        if not any(math.isfinite(value) for value in values):
            continue
        charts.append(Chart(
            name=f"gpu-allocation-{re.sub('[^a-z0-9]+', '-', resource.lower()).strip('-')}",
            title=f"Allocated GPUs · {resource}",
            xlabel=ELAPSED_TIME,
            ylabel=f"Allocated GPUs ({resource})",
            series=(Series(
                "Observed", tuple(float(row[ELAPSED_TIME]) for row in rows),
                values, (None,) * len(rows), records,
            ),),
            kind="step",
            metric=f"gpu_allocation_{resource}",
        ))
    return charts
