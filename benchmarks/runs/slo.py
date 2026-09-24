# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Search observed request concurrency under user-provided service-level constraints."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from evalscope.perf.sla.sla_run import parse_sla_params

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_slo_results
from benchmarks.results.output import (
    BenchmarkRun,
    LocalDirectorySink,
    ResultOutputs,
    ResultSink,
    WandbSink,
    wandb_group_name,
    write_json,
)
from benchmarks.results.wandb import publish_slo_wandb
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
) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for name in criteria:
        samples = [_metric_value(metrics, name) for metrics in metrics_list]
        if any(value is None for value in samples):
            values[name] = None
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
    criteria: dict[str, str],
    average_values: dict[str, float | None],
    metrics_list: list[dict[str, Any]],
) -> bool:
    """Reuse EvalScope comparisons for complete, successful repeated probes."""
    if not all(item["success_rate"] == 1.0 for item in metrics_list):
        return False
    return all(
        average_values[name] is not None and rule.validate(average_values[name])
        for name, rule in parse_sla_params([criteria])[0].items()
    )


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
        """Execute one repetition with the limit owned by its generated or trace scheduler."""
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
        probe_benchmark = (
            replace(probe_config, trace=replace(probe_config.trace, max_concurrency=value))
            if probe_config.trace.trace_selector
            else replace(probe_config, load=replace(probe_config.load, max_concurrency=value))
        )
        return run_benchmark_point(
            probe_benchmark,
            self.service,
            label=label,
            output_dir=probe_dir,
            wandb_group=wandb_group,
        )

    def _search_group(
        self,
        group_index: int,
        criteria: dict[str, str],
        base_dir: str,
        wandb_group: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], BenchmarkRun | None]:
        """Expand one criterion group, stopping on observed load plateau or refining a failed bound."""
        probes: list[dict[str, Any]] = []
        best_run: BenchmarkRun | None = None
        best_peak: int | None = None
        best_limit: int | None = None

        def evaluate(value: int) -> tuple[bool, bool]:
            """Evaluate repeated probes and compare request peaks only when the limit increases."""
            nonlocal best_run, best_peak, best_limit
            runs = [
                self._run_probe(value, group_index, index, criteria, base_dir, wandb_group)
                for index in range(self.benchmark.slo.num_runs)
            ]
            metrics = [run.metrics for run in runs]
            average_values = _average_metric_values(metrics, criteria)
            satisfied = _check_slo(criteria, average_values, metrics)
            peaks = [item["request_concurrency"]["peak"] for item in metrics]
            peak = max(peaks)
            previous = probes[-1] if probes else None
            no_growth = (
                all(item["success_rate"] == 1.0 for item in metrics)
                and previous is not None
                and value > previous["max_concurrency"]
                and peak <= previous["peak_request_concurrency"]
            )
            if satisfied and (
                best_peak is None or peak > best_peak
                or (peak == best_peak and value < best_limit)
            ):
                best_run, best_peak, best_limit = runs[-1], peak, value
            slo_values = [item.get("slo") or {} for item in metrics]
            probes.append({
                "group": group_index,
                "max_concurrency": value,
                "peak_request_concurrency": peak,
                "repeat_peak_request_concurrency": peaks,
                "request_rate": self.benchmark.load.arrival_rate,
                "request_budget": self.benchmark.load.request_count,
                "criteria": criteria,
                "average_values": average_values,
                "slo_attainment": _mean_optional(slo_values, "slo_attainment"),
                "request_goodput": _mean_optional(slo_values, "request_goodput"),
                "token_goodput": _mean_optional(slo_values, "token_goodput"),
                "satisfied": satisfied,
            })
            return satisfied, no_growth

        start = self.benchmark.slo_search_start()
        low = self.benchmark.slo.lower_bound
        high = self.benchmark.slo.upper_bound
        satisfied, _ = evaluate(start)
        stop_reason = "slo_boundary_found"
        if satisfied:
            low = start + 1
            current = start
            stop_reason = "upper_bound_reached"
            # The workload budget and arrivals stay fixed. A larger configured
            # limit is useful only while it produces a higher measured request peak.
            while high is None or current < high:
                value = current * 2 if high is None else min(current * 2, high)
                satisfied, no_growth = evaluate(value)
                if no_growth:
                    stop_reason = "observed_concurrency_not_increasing"
                    break
                if not satisfied:
                    high = value - 1
                    stop_reason = "slo_boundary_found"
                    break
                low = value + 1
                current = value
        else:
            high = start - 1

        if stop_reason == "slo_boundary_found":
            while low <= high:
                value = (low + high) // 2
                satisfied, no_growth = evaluate(value)
                if no_growth:
                    stop_reason = "observed_concurrency_not_increasing"
                    break
                if satisfied:
                    low = value + 1
                else:
                    high = value - 1

        summary = {
            "group": group_index,
            "criteria": criteria,
            "best_peak_request_concurrency": best_peak,
            "best_max_concurrency": best_limit,
            "last_peak_request_concurrency": probes[-1]["peak_request_concurrency"],
            "last_max_concurrency": probes[-1]["max_concurrency"],
            "stop_reason": stop_reason,
        }
        return probes, summary, best_run

    def run(self) -> BenchmarkRun:
        """Publish search decisions separately from the request measurements owned by each probe."""
        if not self.benchmark.slo.params:
            raise ValueError("--slo-params is required for SLO auto-tune")
        wandb_group = wandb_group_name(self.benchmark, self.service)
        record = {
            "mode": "slo_search",
            "model": self.service.model,
            "label": self.label,
            "concurrency_limit_unit": "conversations" if self.benchmark.is_multi_turn else "requests",
        }

        def sinks(directory: str) -> list[ResultSink]:
            """Open summary destinations; resource observations belong to the individual probes."""
            selected: list[ResultSink] = []
            if self.benchmark.outputs.includes("local"):
                selected.append(LocalDirectorySink(directory))
            if self.benchmark.outputs.includes("wandb"):
                selected.append(WandbSink(
                    self.benchmark,
                    execution_dir=directory,
                    run_name=f"{self.benchmark.wandb.run_name or self.service.model}_slo_{self.label or Path(directory).name}",
                    group=wandb_group,
                    publisher=publish_slo_wandb,
                    run_config={**self.benchmark.to_dict(), **record},
                ))
            return selected

        with ResultOutputs(
            self.benchmark, None, output_dir=self.output_dir,
            directory_prefix="slo-", sink_factory=sinks,
        ) as outputs:
            outputs.open(record)
            probes: list[dict[str, Any]] = []
            groups: list[dict[str, Any]] = []
            winning_run: BenchmarkRun | None = None
            for group_index, criteria in enumerate(self.benchmark.slo.params):
                group_probes, summary, best_run = self._search_group(
                    group_index, criteria, outputs.execution_dir, wandb_group,
                )
                probes.extend(group_probes)
                groups.append(summary)
                if best_run is not None:
                    winning_run = best_run
            search = {
                "concurrency_limit_unit": record["concurrency_limit_unit"],
                "probes": probes,
                "groups": groups,
            }
            artifact = write_json(outputs.execution_dir, "slo_results.json", search)
            run = BenchmarkRun(
                record=record,
                metrics={
                    **(winning_run.metrics if winning_run is not None else {"success_num": 0}),
                    "slo_search": search,
                },
                measurements=None,
                artifacts={"slo_results": artifact},
                exit_code=0 if winning_run is not None else 1,
            )
            log_slo_results(search)
            outputs.publish(run)
            if winning_run is None:
                raise ValueError("no SLO probe satisfied the configured criteria")
        return run
