# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compare recorded warmup and measured summary statistics on a common chart."""

from __future__ import annotations

import math
from typing import Any

from benchmarks.results.plots.data import Chart, Series


def phase_summary_charts(measured: dict[str, Any], warmup: dict[str, Any]) -> list[Chart]:
    """Compare phase summaries with the same aggregate statistics used by reports."""
    if "latency" in measured:
        fields = (("latency", "E2EL (s)"), ("ttft", "TTFT (s)"), ("tpot", "TPOT (s)"))
        values = [(name, measured[field].get("p95"), warmup.get(field, {}).get("p95")) for field, name in fields]
    else:
        fields = (("e2e_s", "E2E (s)"), ("queue_wait_s", "Queue wait (s)"), ("server_generation_s", "Server generation (s)"))
        values = [(name, measured.get(field), warmup.get(field)) for field, name in fields]
    rows = [item for item in values if item[1] is not None or item[2] is not None]
    if not rows:
        return []
    return [Chart(
        "phase-summary", "Warmup vs measurement", "Metric", "Value",
        (Series("Warmup", tuple(float(index) for index, _ in enumerate(rows)), tuple(float(item[2]) if item[2] is not None else math.nan for item in rows), (None,) * len(rows), tuple({"phase": "warmup", "metric": item[0]} for item in rows)),
         Series("Measurement", tuple(float(index) for index, _ in enumerate(rows)), tuple(float(item[1]) if item[1] is not None else math.nan for item in rows), (None,) * len(rows), tuple({"phase": "measurement", "metric": item[0]} for item in rows))),
        tick_labels=tuple(item[0] for item in rows), kind="scatter", metric="phase-summary",
    )]
