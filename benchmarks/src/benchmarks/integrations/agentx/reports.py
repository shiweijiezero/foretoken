# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read official AgentX measurements without importing the harness runtime."""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from benchmarks.config.plot import interactivity_percentile
from benchmarks.integrations.agentx import HARNESS_VERSION

_FULL_RESPONSE_ITL = "full_response_inter_token_latency"
_ITL = "inter_token_latency"
_TIME_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "sec": 1.0, "s": 1.0}
_SUMMARY_STATS = ("mean", "p50", "p90", "p95", "p99")


@dataclass(frozen=True)
class _ObservedRecords:
    """Counts and complete successful usage retained from a streaming native record scan."""

    successes: int
    failures: int
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    full_response_seen: bool


def read_metrics(
    directory: Path, *, gpu_count: int | None, concurrency: int | None,
) -> dict[str, Any]:
    """Normalize one native run for result publication, retaining AgentX timing semantics.

    Native summary rates and counts take precedence over observed records.
    Timings retain the native successful-request distributions and completion
    boundaries in the AgentX namespace. Missing reports retain observed request
    counts and unavailable metrics for the runner's failure diagnostics.
    """
    summary = _summary(directory)
    if concurrency is None:
        concurrency = _recorded_concurrency(summary)
    for name, value in (("gpu_count", gpu_count), ("concurrency", concurrency)):
        if value is not None and value < 1:
            raise ValueError(f"{name} must be positive when supplied")
    observed = _observe_records(directory)
    reported_success = _count(summary, "request_count")
    failed_count = _count(summary, "error_request_count")
    success_count = observed.successes if reported_success is None else reported_success
    failed_count = observed.failures if failed_count is None else failed_count
    duration = _seconds(summary.get("benchmark_duration"))
    metadata = summary.get("metadata", {})
    itl_source = _itl_source(summary, observed.full_response_seen)
    throughput = {
        "requests_per_second": _scalar(summary, "request_throughput", "requests/sec"),
        "generation_tokens_per_second": _scalar(summary, "output_token_throughput", "tokens/sec"),
        "prompt_tokens_per_second": _scalar(summary, "input_token_throughput", "tokens/sec"),
        "total_tokens_per_second": _scalar(summary, "total_token_throughput", "tokens/sec"),
    }
    cache_rate, uncached_rate = _cache_rates(
        observed, reported_success, throughput["prompt_tokens_per_second"],
    )
    throughput["cached_prompt_tokens_per_second"] = cache_rate
    throughput["uncached_prompt_tokens_per_second"] = uncached_rate
    for name in (
        "generation_tokens_per_second", "prompt_tokens_per_second", "total_tokens_per_second",
        "cached_prompt_tokens_per_second", "uncached_prompt_tokens_per_second",
    ):
        value = throughput[name]
        throughput[f"{name}_per_gpu"] = value / gpu_count if value is not None and gpu_count is not None else None
    output_rate = throughput["generation_tokens_per_second"]
    throughput["generation_tokens_per_second_per_user"] = (
        output_rate / concurrency if output_rate is not None and concurrency is not None else None
    )
    request_count = success_count + failed_count
    result = {
        "request_num": request_count, "success_num": success_count, "failed_num": failed_count,
        "total_output_tokens": observed.output_tokens if observed.successes == reported_success else None,
        "success_rate": success_count / request_count if request_count else None,
        "benchmark_time": duration, "gpu_count": gpu_count, "max_concurrency": concurrency,
        "throughput": throughput,
        "agentx": {
            "harness_version": HARNESS_VERSION,
            "scenario": metadata.get("scenario"),
            "submission_valid": metadata.get("submission_valid"),
            "submission_invalid_reasons": metadata.get("submission_invalid_reasons"),
            "was_cancelled": summary.get("was_cancelled"),
            "summary_available": bool(summary),
            "interactivity_source": itl_source,
            "timed_failures_included": False,
        },
    }
    for name, tag in (("latency", "request_latency"), ("ttft", "time_to_first_token"), ("itl", itl_source)):
        metric = summary.get(tag, {})
        stats = dict.fromkeys((*_SUMMARY_STATS, *(key for key in metric if key.startswith("p"))))
        for stat in stats:
            result[f"agentx_{name}_{stat}_seconds"] = _seconds(metric, "avg" if stat == "mean" else stat)
            if name == "itl" and stat.startswith("p"):
                interval = result[f"agentx_{name}_{stat}_seconds"]
                result[f"agentx_interactivity_{stat}"] = 1 / interval if interval is not None and interval > 0 else None
    return result


def enrich_interactivity(
    directory: Path, metrics: dict[str, Any], selected: Collection[str],
) -> dict[str, Any]:
    """Derive missing AgentX percentiles from successful profiling records.

    Official summary quantiles and previously retained values take precedence.
    A known native observation count must match the raw timing coverage before
    a new percentile is calculated. Warmup observations never enter the sample.
    """
    result = dict(metrics)
    requested = {}
    for name in selected:
        choice = interactivity_percentile(name)
        if choice is not None and choice[0] == "agentx_interactivity" and _finite(result.get(name)) is None:
            requested[name] = choice[1]
    if not requested:
        return result
    summary = _summary(directory)
    source = metrics.get("agentx", {}).get("interactivity_source")
    source = source or _itl_source(summary, False)
    native = summary.get(source, {})
    missing = {}
    for name, percentile in sorted(requested.items(), key=lambda item: item[1]):
        stat = name.removeprefix("agentx_interactivity_")
        if percentile in (0, 100):
            stat = "min" if percentile == 0 else "max"
        interval = _seconds(native, stat)
        if interval is not None:
            result[name] = 1 / interval if interval > 0 else None
        else:
            missing[name] = percentile
    if missing:
        values = [value for row in _profiling_records(directory)
                  if row.get("error") is None
                  and (value := _seconds(row.get("metrics", {}).get(source), "value")) is not None]
        expected = _finite(native.get("count"))
        complete = bool(values) and (expected is None or expected == len(values))
        quantiles = np.percentile(values, list(missing.values()), method="linear") if complete else ()
        result.update(dict.fromkeys(missing))
        result.update({name: 1 / float(value) if value > 0 else None
                       for name, value in zip(missing, quantiles)})
    return result


def _summary(directory: Path) -> dict[str, Any]:
    """Read the single-run native summary, whose top-level metrics exclude warmup."""
    path = directory / "profile_export_aiperf.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _recorded_concurrency(summary: dict[str, Any]) -> int | None:
    """Read a single recorded concurrency shared by the native profiling phases."""
    phases = [phase for phase in summary.get("input_config", {}).get("phases", [])
              if phase.get("kind") == "profiling"]
    values = {phase.get("concurrency") for phase in phases}
    if len(values) == 1:
        value = values.pop()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _profiling_records(directory: Path) -> Iterator[dict[str, Any]]:
    """Read profiling observations, retaining complete rows before an interrupted JSONL tail."""
    path = directory / "profile_export.jsonl"
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    return
                raise
            if record["metadata"]["benchmark_phase"] == "profiling":
                yield record


def _itl_source(summary: dict[str, Any], full_response_seen: bool) -> str:
    """Keep one full-response or ordinary ITL definition across a run's percentile calculations."""
    return _FULL_RESPONSE_ITL if _FULL_RESPONSE_ITL in summary or full_response_seen else _ITL


def _finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _scalar(summary: dict[str, Any], name: str, unit: str) -> float | None:
    """Decode one native scalar without inferring an absent measurement or its units."""
    metric = summary.get(name, {})
    value = _finite(metric.get("avg"))
    if value is not None and metric["unit"] != unit:
        raise ValueError(f"Unexpected AgentX unit for {name}: {metric['unit']!r}; expected {unit!r}")
    return value


def _count(summary: dict[str, Any], name: str) -> int | None:
    value = _scalar(summary, name, "requests")
    if value is not None and (value < 0 or not value.is_integer()):
        raise ValueError(f"AgentX {name} must be a non-negative request count")
    return int(value) if value is not None else None


def _seconds(metric: dict[str, Any] | None, stat: str = "avg") -> float | None:
    """Convert native display-unit timings to seconds at the report boundary."""
    if metric is None or (value := _finite(metric.get(stat))) is None:
        return None
    unit = metric["unit"]
    if unit not in _TIME_UNITS:
        raise ValueError(f"Unsupported AgentX timing unit: {unit!r}")
    return value * _TIME_UNITS[unit]


def _observe_records(directory: Path) -> _ObservedRecords:
    """Scan counts and usage without retaining complete records from hour-long profiles."""
    successes = failures = 0
    input_tokens = cached_tokens = output_tokens = 0
    cache_complete = output_complete = True
    full_response_seen = False
    for row in _profiling_records(directory):
        metrics = row["metrics"]
        if row.get("error") is not None:
            failures += 1
            continue
        successes += 1
        full_response_seen = full_response_seen or _FULL_RESPONSE_ITL in metrics
        prompt = _token_usage(metrics, "usage_prompt_tokens")
        cached = _token_usage(metrics, "usage_prompt_cache_read_tokens")
        completion = _token_usage(metrics, "usage_completion_tokens")
        if prompt is None or cached is None or cached > prompt:
            cache_complete = False
        else:
            input_tokens += prompt
            cached_tokens += cached
        if completion is None:
            output_complete = False
        else:
            output_tokens += completion
    return _ObservedRecords(
        successes, failures, input_tokens if cache_complete else None,
        cached_tokens if cache_complete else None, output_tokens if output_complete else None,
        full_response_seen,
    )


def _token_usage(metrics: dict[str, Any], name: str) -> int | None:
    """Read explicit non-negative integer token usage without tokenizer estimates."""
    metric = metrics.get(name, {})
    value = _finite(metric.get("value"))
    if value is None or value < 0 or not value.is_integer():
        return None
    if metric["unit"] != "tokens":
        raise ValueError(f"Unexpected AgentX token unit for {name}: {metric['unit']!r}")
    return int(value)


def _cache_rates(
    observed: _ObservedRecords, expected: int | None, input_rate: float | None,
) -> tuple[float | None, float | None]:
    """Split the official input rate using fully covered successful cache usage.

    The native throughput window can differ from the first-to-last-request
    duration, so both cache classes retain its authoritative rate denominator.
    """
    if (
        expected is None or observed.successes != expected or observed.input_tokens is None
        or observed.cached_tokens is None or input_rate is None
    ):
        return None, None
    if observed.input_tokens == 0:
        return (0.0, 0.0) if input_rate == 0 else (None, None)
    cached_rate = input_rate * observed.cached_tokens / observed.input_tokens
    return cached_rate, input_rate - cached_rate
