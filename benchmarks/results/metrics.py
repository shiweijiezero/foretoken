# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Aggregate per-request measurements into benchmark metrics."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import numpy as np


@dataclass(frozen=True)
class RequestMeasurement:
    """Client-side observation of one model request, shared by every benchmark path.

    ``started_at`` is seconds from the start of the run to the request send.
    Streaming engines may provide TTFT, TPOT, and inter-token latency samples;
    non-streamed or failed requests leave them empty. ``conversation_id`` and
    ``turn`` identify the conversation and its request index when the path tracks
    them; EvalScope records leave both unset.
    """

    started_at: float
    ttft: float | None
    latency: float
    tpot: float | None
    itl_samples: tuple[float, ...]
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    succeeded: bool
    conversation_id: str | None
    turn: int | None
    status_code: int | None = None
    error_message: str | None = None
    dataset: str | None = None
    model: str | None = None
    priority: int | None = None
    request_class: str | None = None
    target_output_tokens: int | None = None


def request_activity_events(
    measurements: list[RequestMeasurement],
) -> list[tuple[float, int]]:
    """Return ordered request starts and finishes for aggregate and time-series concurrency.

    Intervals are half-open: a request finishing at a timestamp leaves before
    another starts there. Zero-duration observations have no active interval.
    """
    return sorted(
        event
        for item in measurements
        if item.latency > 0
        for event in (
            (item.started_at, 1),
            (item.started_at + item.latency, -1),
        )
    )


def percentile_summary(values: list[float]) -> dict[str, float | None]:
    """Compute mean and nearest-rank percentiles, matching EvalScope's estimator."""
    if not values:
        return {"mean": None, "p50": None, "p95": None, "p99": None}
    array = np.asarray(values, dtype=float)
    return {
        "mean": float(np.mean(array)),
        "p50": float(np.percentile(array, 50, method="inverted_cdf")),
        "p95": float(np.percentile(array, 95, method="inverted_cdf")),
        "p99": float(np.percentile(array, 99, method="inverted_cdf")),
    }


def compute_tpot(
    latency: float,
    ttft: float | None,
    output_tokens: int | None,
) -> float | None:
    """Compute TPOT from the latency and token count of one streamed request."""
    if ttft is None or output_tokens is None:
        return None
    denominator = int(output_tokens) - 1
    if denominator <= 0:
        return None
    return (latency - ttft) / denominator


def normalized_generation_throughput(
    generation_tokens_per_second: float | None,
    *,
    configured_concurrency: int,
    average_active_requests: float | None,
    gpu_count: int | None,
) -> dict[str, float | None]:
    """Normalize output throughput per user and per GPU.

    A finite ``--max-concurrency`` value represents the configured user count. With
    unlimited concurrency, use the measured time-weighted average active
    requests instead. Return None when throughput or a denominator is missing.
    """
    per_user = None
    per_gpu = None
    if generation_tokens_per_second is not None:
        user_count = (
            float(configured_concurrency)
            if configured_concurrency > 0
            else average_active_requests
        )
        if user_count is not None and user_count > 0:
            per_user = float(generation_tokens_per_second) / user_count
        if gpu_count is not None:
            if gpu_count < 1:
                raise ValueError(f"gpu_count must be >= 1, got {gpu_count}")
            per_gpu = float(generation_tokens_per_second) / gpu_count
    return {
        "generation_tokens_per_second_per_user": per_user,
        "generation_tokens_per_second_per_gpu": per_gpu,
    }


_SLO_CRITERION = re.compile(r"^(?:avg_|p50_|p90_|p95_|p99_)?(latency|ttft|tpot|itl)$")
_SLO_OPERATORS = {
    "<": lambda actual, expected: actual < expected,
    "<=": lambda actual, expected: actual <= expected,
    "==": lambda actual, expected: actual == expected,
    ">=": lambda actual, expected: actual >= expected,
    ">": lambda actual, expected: actual > expected,
}
_SLO_EXPRESSION = re.compile(r"^(<=|>=|==|<|>)\s*(-?(?:\d+(?:\.\d*)?|\.\d+))$")


def request_slo_results(
    measurements: list[RequestMeasurement],
    criteria: dict[str, str] | None,
    total_time: float,
    by_class: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Evaluate each request against its class target or the global fallback."""
    if not criteria and not by_class:
        return None

    def checks_for(target: dict[str, str]) -> list[tuple[str, Any, float]]:
        checks = []
        for name, expression in target.items():
            metric_match = _SLO_CRITERION.fullmatch(name)
            expression_match = _SLO_EXPRESSION.fullmatch(expression)
            if metric_match is None or expression_match is None:
                raise ValueError(
                    "Request-level SLO requires a latency/ttft/tpot/itl criterion, "
                    f"got {name}={expression}"
                )
            checks.append((
                metric_match.group(1),
                _SLO_OPERATORS[expression_match.group(1)],
                float(expression_match.group(2)),
            ))
        return checks

    targets = {name: checks_for(target) for name, target in (by_class or {}).items()}
    # Search criteria may also contain aggregate-only metrics such as rps.
    supported = {
        name: expression for name, expression in (criteria or {}).items()
        if _SLO_CRITERION.fullmatch(name) and _SLO_EXPRESSION.fullmatch(expression)
    }
    global_checks = checks_for(supported) if supported else None
    if not by_class and global_checks is None:
        return {
            "criteria": criteria,
            "request_slo_met": None,
            "slo_attainment": None,
            "request_goodput": None,
            "token_goodput": None,
        }

    request_slo_met: list[bool] = []
    applied_criteria: list[dict[str, str]] = []
    good_requests = 0
    good_tokens = 0
    for item in measurements:
        values = {
            "latency": item.latency,
            "ttft": item.ttft,
            "tpot": item.tpot,
            "itl": max(item.itl_samples) if item.itl_samples else None,
        }
        if item.request_class in targets:
            applied_criteria.append(by_class[item.request_class])
            checks = targets[item.request_class]
        elif global_checks is not None:
            applied_criteria.append(supported)
            checks = global_checks
        else:
            raise ValueError(
                f"No SLO target for request_class {item.request_class!r}; "
                "set --slo-by-class or --slo-params"
            )
        met = item.succeeded
        for metric, operator, expected in checks:
            actual = values[metric]
            met = met and actual is not None and operator(float(actual), expected)
        request_slo_met.append(bool(met))
        if met:
            good_requests += 1
            good_tokens += int(item.output_tokens or 0)

    duration = float(total_time)
    return {
        "criteria": criteria,
        "by_class": by_class,
        "applied_criteria": applied_criteria,
        "request_slo_met": request_slo_met,
        "slo_attainment": good_requests / len(measurements) if measurements else 0.0,
        "request_goodput": good_requests / duration if duration > 0 else None,
        "token_goodput": good_tokens / duration if duration > 0 else None,
    }


def summarize_measurement_groups(
    measurements: list[RequestMeasurement],
    *,
    total_time: float,
    stream: bool,
    arrival_rate: float,
    reported_concurrency: int,
    slo_criteria: dict[str, str] | None,
    slo_by_class: dict[str, dict[str, str]] | None,
    include_single_dataset: bool = False,
) -> dict[str, dict[str, Any]]:
    """Summarize labeled requests on their shared experiment clock without per-group GPU assumptions."""
    result: dict[str, dict[str, Any]] = {}
    dimensions = (
        ("dataset", "datasets"),
        ("model", "models"),
        ("request_class", "request_classes"),
    )
    for field, group_name in dimensions:
        names = sorted({
            value for item in measurements
            if (value := getattr(item, field)) is not None
        })
        if not names:
            continue
        if len(names) == 1 and field != "request_class" and not (
            field == "dataset" and include_single_dataset
        ):
            continue
        result[group_name] = {}
        for name in names:
            subset = [item for item in measurements if getattr(item, field) == name]
            result[group_name][name] = summarize_measurements(
                subset, total_time=total_time, stream=stream,
                arrival_rate=arrival_rate, request_count=len(subset),
                reported_concurrency=reported_concurrency, gpu_count=None,
                include_normalized_throughput=False,
                slo_criteria=slo_criteria, slo_by_class=slo_by_class,
            )
    return result


def summarize_measurements(
    measurements: list[RequestMeasurement],
    *,
    total_time: float,
    stream: bool,
    arrival_rate: float,
    request_count: int,
    reported_concurrency: int,
    gpu_count: int | None,
    include_normalized_throughput: bool = True,
    slo_criteria: dict[str, str] | None = None,
    slo_by_class: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Aggregate request measurements and workload coordinates into the published metrics.

    Latency distributions use successful requests only; throughput divides
    successful request and token counts by ``total_time``. TTFT and TPOT are
    reported only for streamed runs. Inter-token latency is included when the
    request engine records those samples.
    """
    successful = [item for item in measurements if item.succeeded]
    latencies = [item.latency for item in successful]
    ttfts: list[float] = []
    tpots: list[float] = []
    itls: list[float] = []
    if stream:
        ttfts = [item.ttft for item in successful if item.ttft is not None]
        tpots = [item.tpot for item in successful if item.tpot is not None]
        itls = [value for item in successful for value in item.itl_samples]

    input_complete = all(item.input_tokens is not None for item in successful)
    output_complete = all(item.output_tokens is not None for item in successful)
    input_tokens = (
        sum(int(item.input_tokens) for item in successful) if input_complete else None
    )
    output_tokens = (
        sum(int(item.output_tokens) for item in successful) if output_complete else None
    )
    reported_cached_tokens = [
        item.cached_input_tokens
        for item in successful
        if item.cached_input_tokens is not None
    ]
    success_count = len(successful)
    request_num = len(measurements)

    generation_tokens_per_second = (
        output_tokens / total_time if output_tokens is not None else None
    )
    prompt_tokens_per_second = (
        input_tokens / total_time if input_tokens is not None else None
    )
    total_tokens_per_second = (
        (input_tokens + output_tokens) / total_time
        if input_tokens is not None and output_tokens is not None
        else None
    )
    throughput: dict[str, Any] = {
        "requests_per_second": success_count / total_time,
        "generation_tokens_per_second": generation_tokens_per_second,
        "prompt_tokens_per_second": prompt_tokens_per_second,
        "total_tokens_per_second": total_tokens_per_second,
    }
    active = peak_active_requests = 0
    for _, change in request_activity_events(measurements):
        active += change
        peak_active_requests = max(peak_active_requests, active)
    average_active_requests = (
        sum(item.latency for item in measurements) / total_time
        if measurements and total_time > 0
        else None
    )
    if include_normalized_throughput:
        throughput.update(
            normalized_generation_throughput(
                generation_tokens_per_second,
                configured_concurrency=reported_concurrency,
                average_active_requests=average_active_requests,
                gpu_count=gpu_count,
            )
        )
    slo = request_slo_results(measurements, slo_criteria, total_time, slo_by_class)
    return {
        "request_num": request_num,
        "success_num": success_count,
        "failed_num": request_num - success_count,
        "success_rate": success_count / request_num if request_num else 0.0,
        "stream": stream,
        "latency": percentile_summary(latencies),
        "ttft": percentile_summary(ttfts),
        "tpot": percentile_summary(tpots),
        "itl": percentile_summary(itls),
        "throughput": throughput,
        "avg_input_tokens": (
            input_tokens / success_count
            if success_count and input_tokens is not None
            else None
        ),
        "avg_output_tokens": (
            output_tokens / success_count
            if success_count and output_tokens is not None
            else None
        ),
        "avg_cached_input_tokens": (
            sum(reported_cached_tokens) / len(reported_cached_tokens)
            if reported_cached_tokens
            else None
        ),
        "benchmark_time": total_time,
        "request_rate": arrival_rate,
        "num_prompts": request_count,
        "max_concurrency": reported_concurrency,
        "request_concurrency": {
            "peak": peak_active_requests,
            "mean": average_active_requests,
        },
        "slo": slo,
    }
