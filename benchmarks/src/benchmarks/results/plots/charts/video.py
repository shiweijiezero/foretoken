# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project measured and warmup video timings onto their recorded request indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.results.plots.data import Chart, Series, _numeric
from benchmarks.results.recording.database import read_recorded_requests


def _video_phase_charts(source: Path, metrics: dict[str, Any]) -> list[Chart]:
    """Plot measured and warmup video timings as phase-labeled request series."""
    phases = [
        (read_recorded_requests(source, channel), label)
        for channel, label in (("warmup_raw_output", "Warmup"), ("raw_results", "Measurement"))
    ]
    charts: list[Chart] = []
    for field in ("e2e_s", "queue_wait_s", "server_generation_s"):
        series = []
        for rows, label in phases:
            selected = sorted(
                (row for row in rows if row.get("success") and _numeric(row.get(field)) is not None),
                key=lambda row: row["index"],
            )
            if selected:
                series.append(Series(label, tuple(float(row["index"]) for row in selected), tuple(float(row[field]) for row in selected), (None,) * len(selected), tuple(row for row in selected)))
        if series:
            charts.append(Chart(f"video-phase-{field}", f"Video phases · {field}", "Request index", f"{field} (s)", tuple(series), metric=field))
    return charts
