# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read recorded experiment plans and measurements without executing benchmarks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from benchmarks.results.plots.data import Chart, _method
from benchmarks.results.plots.measurements import (
    _quality_charts,
    _slo_charts,
    comparison_charts,
)


@dataclass
class ExperimentResults:
    """Run evidence and comparison inputs consumed by the figure exporter."""

    runs: list[dict[str, Any]]
    points: list[dict[str, Any]]
    summary: list[dict[str, Any]]
    native: list[Chart]
    method_filters: dict[Path, tuple[str, ...]]


@dataclass
class _RecordedRun:
    """One planned measurement, including attempts that produced no metrics."""

    directory: Path
    configuration: Path | None
    conditions: dict[str, Any]
    method: str
    metrics: dict[str, Any] | None
    exit_code: int | None
    started: bool


def _conditions(config: dict[str, Any]) -> dict[str, Any]:
    """Separate saved workload choices from deployment and output locations."""
    conditions = {key: value for key, value in config.items() if key not in {
        "service", "services", "service_choices", "output", "wandb", "sweep", "resolved", "url",
    }}
    if "endpoint" in conditions:
        endpoint = conditions.pop("endpoint")
        conditions.pop("name", None)
        conditions["timeout_s"] = endpoint["timeout_s"]
    for section in ("load", "generation", "dataset"):
        fields = conditions.pop(section, {})
        if isinstance(fields, dict):
            conditions.update(fields)
        else:
            conditions[section] = fields
    slo = conditions.pop("slo", None)
    if isinstance(slo, dict):
        conditions["slo_params"] = slo.get("params")
        if slo.get("search"):
            conditions["slo_search"] = slo
    for field in ("timeout", "max_retries"):
        if field in config.get("service", {}):
            conditions[field] = config["service"][field]
    return conditions


def _leaf(
    directory: Path, configuration: Path, conditions: dict[str, Any], method: str,
    exit_code: int | None, started: bool,
) -> _RecordedRun:
    """Read one measurement, retaining the parent's plan when execution never reached it."""
    if (directory / "config.json").is_file():
        configuration = directory / "config.json"
        conditions = _conditions(json.loads(configuration.read_text()))
        started = True
    metrics_path = directory / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else None
    return _RecordedRun(directory, configuration, conditions, method, metrics, exit_code, started)


def _recorded_runs(directory: Path, context: dict[str, Any]) -> Iterator[_RecordedRun]:
    """Follow the saved sweep/evaluation plan rather than rediscovering nested native reports."""
    configuration = directory / "config.json"
    if not configuration.is_file():
        search_path = directory / "slo_results.json"
        if search_path.is_file():
            # Search summaries have no top-level configuration. Probe records
            # carry the fixed workload; concurrency and criteria belong to the search.
            probe = next(iter(sorted(directory.glob("group-*/max-concurrency-*/run-*/config.json"))), None)
            conditions = _conditions(json.loads(probe.read_text())) if probe is not None else {}
            for field in ("max_concurrency", "slo_params", "slo_search"):
                conditions.pop(field, None)
            search = json.loads(search_path.read_text())
            conditions.update(
                mode="slo_search", concurrency_limit_unit=search["concurrency_limit_unit"],
                slo_params=[group["criteria"] for group in search["groups"]],
            )
            yield _RecordedRun(directory, probe, conditions, "default", None, context.get("exit_code"), True)
        return
    config = json.loads(configuration.read_text())
    if config.get("mode") in {"parameter_sweep", "video_parameter_sweep"}:
        points_path = directory / "sweep_points.json"
        points = json.loads(points_path.read_text()) if points_path.is_file() else []
        observed = {(point["combination"], point["run_number"]): point for point in points}
        for combination in config["combinations"]:
            children = [directory / combination["dir"] / f"run={number}" for number in range(config["num_runs"])]
            bench = {**config["base"], **combination["bench"]}
            method = _method({"bench": bench})
            planned = {**_conditions(config["base"]), **{
                key: value for key, value in combination["bench"].items()
                if key != "service" and not key.startswith("_")
            }}
            # Repetitions share one prepared point configuration. A completed
            # sibling supplies its resolved model and mode for unfinished repeats.
            saved = next((child / "config.json" for child in children if (child / "config.json").is_file()), None)
            if saved is not None:
                planned = _conditions(json.loads(saved.read_text()))
            for number, child in enumerate(children):
                point = observed.get((combination["dir"], number))
                yield _leaf(child, configuration, planned, method,
                            point["exit_code"] if point is not None else None, point is not None)
        return
    metrics_path = directory / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else None
    if metrics is not None and "evaluation_comparison" in metrics:
        # The comparison owns method labels and exit states; child reports own
        # native scores and environment snapshots. Never count the aggregate twice.
        for method in metrics["evaluation_comparison"]["methods"]:
            yield _leaf(directory / method["directory"], configuration, _conditions(config),
                        method["label"], method["exit_code"], method["status"] != "not_started")
        return
    yield _RecordedRun(directory, configuration, _conditions(config), _method({"bench": config}),
                       metrics, context.get("exit_code"), True)


def read_experiment(
    source: Path, *, iterations: tuple[str, ...] = (), methods: tuple[str, ...] = (),
) -> ExperimentResults | None:
    """Read experiment/iteration/run wrappers and group performance measurements by saved workload.

    Planned but unstarted measurements remain visible. Performance repetitions use
    the sweep's scalar statistics; native score and comparison charts keep their
    existing axes, method identities, and error semantics.
    """
    command_source = (source / "generated/context.json").is_file()
    if (source / "iterations").is_dir():
        selected = sorted(path for path in (source / "iterations").iterdir() if path.is_dir())
    elif (source / "runs").is_dir():
        selected = [source]
    elif command_source:
        selected = [source.parent.parent]
    else:
        return None
    unknown = set(iterations) - {path.name for path in selected}
    if unknown:
        raise ValueError("Unknown plot iterations: " + ", ".join(sorted(unknown)))
    selected = [path for path in selected if not iterations or path.name in iterations]
    runs: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    native: dict[tuple[Any, ...], Chart] = {}
    combinations: dict[tuple[str, Path | None], str] = {}
    repetition_counts: dict[tuple[str, str], dict[str, int]] = {}
    available_methods: set[str] = set()
    method_filters: dict[Path, tuple[str, ...]] = {}
    for iteration in selected:
        contexts = ([source / "generated/context.json"] if command_source
                    else sorted((iteration / "runs").glob("*/generated/context.json")))
        for context_path in contexts:
            context = json.loads(context_path.read_text())
            command = context_path.parent.parent
            identity = {
                "iteration": iteration.name, "run": command.name,
                "status": context["status"], "exit_code": context.get("exit_code"),
                "command": context["command"], "context": str(context_path),
            }
            found = False
            for directory in sorted((command / "artifacts").glob("*")):
                if not directory.is_dir():
                    continue
                for saved in _recorded_runs(directory, context):
                    measured = saved.metrics or {}
                    comparison = comparison_charts(measured)
                    charts = []
                    if comparison is not None:
                        charts, identities = comparison
                    elif "scores" in measured:
                        charts, identities = _quality_charts(measured), [saved.method]
                    elif (saved.directory / "slo_results.json").is_file():
                        charts = _slo_charts(json.loads((saved.directory / "slo_results.json").read_text()))
                        identities = [saved.method]
                    else:
                        identities = [saved.method]
                    available_methods.update(identities)
                    chosen = [method for method in identities if not methods or method in methods]
                    if not chosen:
                        continue
                    found = True
                    if comparison is not None and methods:
                        method_filters[saved.directory] = tuple(chosen)
                    label = f"{iteration.name} / {saved.method}"
                    # A summary without its probe configuration is readable, but
                    # its unknown workload cannot be equated with another run.
                    fixed = (json.dumps(saved.conditions, sort_keys=True),
                             saved.directory if saved.configuration is None else None)
                    combination = combinations.setdefault(fixed, f"workload-{len(combinations) + 1}")
                    runs.append({**identity, "method": saved.method, "methods": chosen,
                                 "result": str(saved.directory), "measured": saved.metrics is not None or bool(charts),
                                 "started": saved.started, "measurement_exit_code": saved.exit_code,
                                 "config": str(saved.configuration) if saved.configuration is not None else None,
                                 "conditions": saved.conditions,
                                 "environment": str(saved.directory / "environment.json")
                                 if (saved.directory / "environment.json").is_file() else None,
                                 **{key: measured.get(key) for key in ("request_num", "success_num", "failed_num")}})
                    if charts:
                        for chart in charts:
                            series = []
                            for item in chart.series:
                                method = str(item.records[0].get("method") or item.name) if comparison is not None else saved.method
                                if method not in chosen:
                                    continue
                                name = f"{iteration.name} / {method} / {command.name}"
                                if comparison is not None and item.name != method:
                                    name += f" / {item.name}"
                                series.append(replace(item, name=name, records=tuple(
                                    {**row, "method": f"{iteration.name} / {method}", "result": str(saved.directory)}
                                    for row in item.records
                                )))
                            if not series:
                                continue
                            key = (chart.name, chart.title, chart.ylabel, chart.tick_labels, fixed)
                            if key in native:
                                native[key] = replace(native[key], series=native[key].series + tuple(series))
                            else:
                                native[key] = replace(chart, name=f"experiment-{combination}-{chart.name}", series=tuple(series))
                    else:
                        counts = repetition_counts.setdefault((label, combination), {"runs": 0, "requested_runs": 0, "failed_runs": 0})
                        counts["runs"] += int(saved.started)
                        counts["requested_runs"] += 1
                        counts["failed_runs"] += int(bool(saved.exit_code) or bool(measured.get("failed_num")))
                        points.append({**measured, "combination": combination,
                                       "parameter_group": str(saved.conditions.get("mode", "performance")),
                                       "bench": {**saved.conditions, "service": {"name": label}},
                                       "exit_code": saved.exit_code})
            if not found and not methods:
                runs.append({**identity, "result": None, "measured": False})
    unknown = set(methods) - available_methods
    if unknown:
        raise ValueError("Unknown plot methods: " + ", ".join(sorted(unknown)))
    from benchmarks.sweeps.core import summarize_sweep

    summary = summarize_sweep(points)
    for row in summary:
        row.update(repetition_counts[row["method"], row["combination"]])
    return ExperimentResults(runs, points, summary, list(native.values()), method_filters)
