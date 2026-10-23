# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind published metric namespaces to their measured W&B horizontal axes."""

from typing import Any

from benchmarks.results.measurements.timeseries import ELAPSED_TIME, REQUEST_INDEX

TRACE_TIME = "Scheduled trace time (s)"
_AXIS_METRICS = {
    ELAPSED_TIME: ("Time/*", "Cumulative/*", "datasets/*", "models/*", "request_classes/*", "Replicas/*"),
    REQUEST_INDEX: ("Requests/*",),
    TRACE_TIME: ("Trace/*",),
    "Evaluation/Elapsed (s)": (
        "Evaluation/Generations*", "Evaluation/Likelihood windows*",
        "Evaluation/Predictions*", "Evaluation/Reviews*",
    ),
    "video/completed_requests": ("video/request/*", "video/successful_requests", "video/failed_requests"),
}


def define_axis_metrics(session: Any, axis: str) -> None:
    """Define one live or restored metric domain without carrying values across log rows."""
    session.define_metric(axis, hidden=True)
    for name in _AXIS_METRICS[axis]:
        session.define_metric(name, step_metric=axis, step_sync=False)
