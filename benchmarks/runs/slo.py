# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Search workload capacity under user-provided service-level constraints."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_slo_results
from benchmarks.results.output import (
    BenchmarkRun,
    result_directory_path,
    wandb_group_name,
    write_json,
)
from benchmarks.runs.dispatch import run_benchmark_point


_SLO_ALIASES = {
    "latency.mean": "avg_latency",
    "ttft.mean": "avg_ttft",
    "tpot.mean": "avg_tpot",
    "throughput.requests_per_second": "rps",
    "throughput.generation_tokens_per_second": "tps",
}


def _metric_value(metrics: dict[str, Any], name: str) -> float | None:
    name = _SLO_ALIASES.get(name, name)
    if name in {"avg_latency", "avg_ttft", "avg_tpot", "rps", "tps"}:
        if name == "rps":
            value = metrics["throughput"].get("requests_per_second")
        elif name == "tps":
            value = metrics["throughput"].get("generation_tokens_per_second")
        else:
            value = metrics[name.removeprefix("avg_")]["mean"]
        return float(value) if value is not None else None
    for prefix in ("p50", "p90", "p95", "p99"):
        if name == f"{prefix}_latency":
            value = metrics["latency"].get(prefix)
            return float(value) if value is not None else None
        if name == f"{prefix}_ttft":
            value = metrics["ttft"].get(prefix)
            return float(value) if value is not None else None
        if name == f"{prefix}_tpot":
            value = metrics["tpot"].get(prefix)
            return float(value) if value is not None else None
    raise ValueError(f"unknown SLO metric: {name}")


def _average_metric_values(
    metrics_list: list[dict[str, Any]], criteria: dict[str, str]
) -> dict[str, float]:
    values: dict[str, float] = {}
    for name in criteria:
        samples = [_metric_value(metrics, name) for metrics in metrics_list]
        if any(value is None for value in samples):
            values[name] = float("nan")
        else:
            values[name] = sum(float(value) for value in samples) / len(samples)
    return values


def _mean_optional(values: list[dict[str, Any]], key: str) -> float | None:
    """Average a diagnostic SLO value when every repeated run reports it."""
    samples = [value.get(key) for value in values]
    if not samples or any(sample is None for sample in samples):
        return None
    return sum(float(sample) for sample in samples) / len(samples)


def _check_slo(
    parallel: int,
    criteria: dict[str, str],
    average_values: dict[str, float],
    metrics_list: list[dict[str, Any]],
) -> bool:
    """Reuse EvalScope ``check_sla`` for pass/fail and criterion comparison logs."""
    from evalscope.perf.sla import sla_run
    from evalscope.perf.sla.sla_run import check_sla, parse_sla_params
    from evalscope.perf.utils.perf_models import BenchmarkSummary

    success = all(float(item["success_rate"]) >= 1.0 for item in metrics_list)
    total = max(int(metrics_list[-1].get("request_num") or 1), 1)
    results = {
        "metrics": BenchmarkSummary(
            total_requests=total,
            succeed_requests=total if success else 0,
            failed_requests=0 if success else total,
        )
    }
    previous = sla_run.get_metric_values
    sla_run.get_metric_values = lambda _: average_values
    try:
        return check_sla(
            results, parse_sla_params([criteria]), f"parallel={parallel}"
        )
    finally:
        sla_run.get_metric_values = previous


class SloAutoTuneBenchmark:
    """Search client concurrency under the configured arrival process and publish each probe."""

    def __init__(
        self,
        benchmark: BenchmarkConfig,
        service: ModelService,
        *,
        label: str = "",
        output_dir: str | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.service = service
        self.label = label
        self.output_dir = output_dir

    def _run_probe(
        self,
        value: int,
        group_index: int,
        run_index: int,
        criteria: dict[str, str],
        base_dir: str,
        wandb_group: str,
    ) -> BenchmarkRun:
        probe_config = replace(
            self.benchmark,
            slo=replace(self.benchmark.slo, params=[criteria]),
        )
        label = f"slo-group-{group_index}-concurrency-{value}-run-{run_index + 1}"
        probe_dir = os.path.join(
            base_dir,
            f"group-{group_index}",
            f"max-concurrency-{value}",
            f"run-{run_index + 1}",
        )
        probe_benchmark = replace(
            probe_config,
            load=replace(probe_config.load, max_concurrency=value),
        )
        return run_benchmark_point(
            probe_benchmark,
            self.service,
            label=label,
            output_dir=probe_dir,
            wandb_group=wandb_group,
        )

    def run(self) -> BenchmarkRun:
        """Binary-search the configured client-concurrency variable for each criterion group."""
        if not self.benchmark.slo.params:
            raise ValueError("--slo-params is required for SLO auto-tune")
        base_dir = result_directory_path(
            self.benchmark, self.output_dir, "slo-"
        )
        os.makedirs(base_dir, exist_ok=True)
        wandb_group = wandb_group_name(self.benchmark, self.service)
        summaries: list[dict[str, Any]] = []
        winning_run: BenchmarkRun | None = None
        start = self.benchmark.slo_search_start()
        for group_index, criteria in enumerate(self.benchmark.slo.params):
            cache: dict[int, tuple[BenchmarkRun, list[dict[str, Any]]]] = {}

            def probe(value: int) -> tuple[BenchmarkRun, list[dict[str, Any]]]:
                if value not in cache:
                    runs = [
                        self._run_probe(
                            value, group_index, run_index, criteria, base_dir, wandb_group
                        )
                        for run_index in range(self.benchmark.slo.num_runs)
                    ]
                    cache[value] = (runs[-1], [run.metrics for run in runs])
                return cache[value]

            low = self.benchmark.slo.lower_bound
            high = self.benchmark.slo.upper_bound
            best = None

            def evaluate(value: int) -> bool:
                nonlocal winning_run, best
                run, metrics_list = probe(value)
                average_values = _average_metric_values(metrics_list, criteria)
                passed = _check_slo(
                    value, criteria, average_values, metrics_list
                )
                if passed:
                    best = value
                    winning_run = run
                slo_values = [
                    metrics.get("slo") or {}
                    for metrics in metrics_list
                ]
                summaries.append(
                    {
                        "group": group_index,
                        "search_variable": "max_concurrency",
                        "max_concurrency": value,
                        "request_rate": self.benchmark.load.arrival_rate,
                        "request_budget": self.benchmark.load.request_count,
                        "criteria": criteria,
                        "average_values": average_values,
                        "slo_attainment": _mean_optional(slo_values, "slo_attainment"),
                        "request_goodput": _mean_optional(slo_values, "request_goodput"),
                        "token_goodput": _mean_optional(slo_values, "token_goodput"),
                        "satisfied": passed,
                    }
                )
                return passed

            if evaluate(start):
                low = start + 1
                if high is None:
                    probe_value = max(start * 2, start + 1)
                    while evaluate(probe_value):
                        low = probe_value + 1
                        probe_value *= 2
                    high = probe_value - 1
            else:
                high = start - 1
            while low <= high:
                value = (low + high) // 2
                if evaluate(value):
                    low = value + 1
                else:
                    high = value - 1
            summaries.append(
                {
                    "group": group_index,
                    "criteria": criteria,
                    "max_satisfied": best,
                }
            )
        if winning_run is None:
            raise ValueError("no SLO probe satisfied the configured criteria")
        artifact = write_json(base_dir, "slo_results.json", {"probes": summaries})
        if not self.benchmark.outputs.includes("quiet"):
            log_slo_results({"probes": summaries})
        winning_run.artifacts["slo_results"] = artifact
        return winning_run
