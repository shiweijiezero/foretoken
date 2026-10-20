# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project recorded SLO search probes without rerunning the search algorithm."""

from __future__ import annotations

from typing import Any

from benchmarks.results.plots.data import Chart, Series, _numeric


def _slo_charts(search: dict[str, Any]) -> list[Chart]:
    """Plot the search's saved probe averages, retaining each probe decision in tables."""
    charts = []
    for group in dict.fromkeys(probe["group"] for probe in search["probes"]):
        rows = sorted(
            (row for row in search["probes"] if row["group"] == group),
            key=lambda row: row["max_concurrency"],
        )
        fields = list(
            dict.fromkeys(field for row in rows for field in row["average_values"])
        )
        for field in fields + [
            "slo_attainment",
            "request_goodput",
            "token_goodput",
            "peak_request_concurrency",
        ]:
            chosen = [
                row
                for row in rows
                if _numeric(
                    row["average_values"].get(field)
                    if field in fields
                    else row.get(field)
                )
                is not None
            ]
            if chosen:
                charts.append(
                    Chart(
                        f"slo-group-{group}-{field.replace('_', '-')}",
                        f"SLO group {group} · {field}",
                        f"Configured concurrency ({search['concurrency_limit_unit']})",
                        field,
                        (
                            Series(
                                "Probe average",
                                tuple(float(row["max_concurrency"]) for row in chosen),
                                tuple(
                                    float(
                                        row["average_values"].get(field)
                                        if field in fields
                                        else row[field]
                                    )
                                    for row in chosen
                                ),
                                (None,) * len(chosen),
                                tuple(chosen),
                            ),
                        ),
                        metric=field,
                    )
                )
    return charts
