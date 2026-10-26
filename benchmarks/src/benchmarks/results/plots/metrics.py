# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Derive offline chart metrics from complete saved HTTP measurement evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from benchmarks.results.plots.data import _numeric
from benchmarks.results.recording.database import read_records
from benchmarks.results.recording.requests import (
    load_http_measurements,
    read_request_records,
)


DERIVED_PLOT_METRICS = frozenset({
    "decode_interactivity_p90",
    "total_tokens_per_second_per_gpu",
    "cached_prompt_tokens_per_second_per_gpu",
    "uncached_prompt_tokens_per_second_per_gpu",
})


def enrich_plot_metrics(
    directory: Path, metrics: dict[str, Any], *, gpu_count: int | None = None,
) -> dict[str, Any]:
    """Return chart-ready HTTP metrics without changing saved measurements.

    Saved measured values retain precedence; unavailable values may be derived.
    Request-level TPOT supplies decode interactivity; chunk-level ITL quantiles
    describe a different distribution.
    Token accounting requires all successful records and their relevant usage.
    A supplied GPU count fills missing capacity but cannot replace recorded capacity.
    """
    measurements = load_http_measurements(read_request_records(directory))
    successful = [item for item in measurements if item.succeeded]
    recorded_capacity = metrics.get("gpu_count")
    if "gpu_count" not in metrics:
        environment = read_records(directory, ("environment.json",)).get("environment.json", {})
        before = environment.get("before", {})
        models = {item.model for item in measurements if item.model is not None}
        if len(models) <= 1 and (not models or not before.get("model") or before["model"] in models):
            recorded_capacity = before.get("declared_gpu_count")
    capacity = _gpu_capacity(recorded_capacity, gpu_count)
    throughput = dict(metrics.get("throughput", {}))
    result = {**metrics, "throughput": throughput}
    if capacity is not None:
        result["gpu_count"] = capacity
    complete = bool(successful) and len(successful) == metrics.get("success_num")
    duration = _numeric(metrics.get("benchmark_time"))
    rates = {
        "generation_tokens_per_second": _numeric(throughput.get("generation_tokens_per_second")),
        "total_tokens_per_second": _numeric(throughput.get("total_tokens_per_second")),
        "cached_prompt_tokens_per_second": None,
        "uncached_prompt_tokens_per_second": None,
    }
    interactivity = None
    if complete:
        # One-token responses have no decode interval; missing usage cannot
        # establish whether an unmeasured interval should belong in the sample.
        outputs = [_numeric(item.output_tokens) for item in successful]
        if all(value is not None for value in outputs):
            decode = [item for item in successful if item.output_tokens > 1]
            tpots = [_numeric(item.tpot) for item in decode]
            if tpots and all(value is not None and value >= 0 for value in tpots):
                p90 = float(np.percentile(tpots, 90, method="inverted_cdf"))
                if p90 > 0:
                    interactivity = 1 / p90

        if duration is not None and duration > 0:
            inputs = [_numeric(item.input_tokens) for item in successful]
            cached = [_numeric(item.cached_input_tokens) for item in successful]
            inputs_complete = all(value is not None and value >= 0 for value in inputs)
            outputs_complete = all(value is not None and value >= 0 for value in outputs)
            if outputs_complete and rates["generation_tokens_per_second"] is None:
                rates["generation_tokens_per_second"] = sum(outputs) / duration
            if inputs_complete and outputs_complete and rates["total_tokens_per_second"] is None:
                # Prompt usage includes cached tokens already.
                rates["total_tokens_per_second"] = (sum(inputs) + sum(outputs)) / duration
            if inputs_complete and all(
                value is not None and 0 <= value <= total
                for value, total in zip(cached, inputs)
            ):
                rates["cached_prompt_tokens_per_second"] = sum(cached) / duration
                rates["uncached_prompt_tokens_per_second"] = (sum(inputs) - sum(cached)) / duration

    if result.get("decode_interactivity_p90") is None:
        result["decode_interactivity_p90"] = interactivity
    for name, rate in rates.items():
        normalized = f"{name}_per_gpu"
        if throughput.get(normalized) is None:
            throughput[normalized] = rate / capacity if rate is not None and capacity is not None else None
    return result


def _gpu_capacity(recorded: Any, supplied: int | None) -> int | None:
    """Resolve a single explicit GPU capacity for normalized offline metrics."""
    for value in (recorded, supplied):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise ValueError(f"GPU count must be a positive integer, got {value!r}")
    if recorded is not None and supplied is not None and recorded != supplied:
        raise ValueError(f"Supplied GPU count {supplied} conflicts with recorded GPU count {recorded}")
    return recorded if recorded is not None else supplied
