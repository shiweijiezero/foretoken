# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read saved result directories and their execution plans without running benchmarks."""

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
from benchmarks.results.tasks import task_source


@dataclass
class SavedResults:
    """Run evidence and comparison inputs consumed by the figure exporter."""

    runs: list[dict[str, Any]]
    points: list[dict[str, Any]]
    summary: list[dict[str, Any]]
    native: list[Chart]
    method_filters: dict[Path, tuple[str, ...]]
    base_approaches: tuple[str, ...]


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
    status: str | None = None
    context: dict[str, Any] | None = None


def _conditions(config: dict[str, Any]) -> dict[str, Any]:
    """Separate saved workload choices from deployment and output locations."""
    if config.get("mode") == "slo_search" and "base" in config:
        return {**_conditions(config["base"]), "mode": "slo_search"}
    conditions = {key: value for key, value in config.items() if key not in {
        "service", "services", "service_choices", "output", "wandb", "sweep", "resolved", "url", "planned_num",
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


def _environment_settings(directory: Path) -> dict[str, Any] | None:
    """Read existing serving declarations for comparison CSVs, excluding runtime instance identities."""
    environment_path = directory / "environment.json"
    serving_path = directory / "serving_environments.json"
    if not environment_path.is_file() and not serving_path.is_file():
        return None
    environment = json.loads(environment_path.read_text(encoding="utf-8")) if environment_path.is_file() else {}
    settings = {"client": environment.get("client")}
    phases = {phase: environment[phase] for phase in ("before", "after") if phase in environment}
    if serving_path.is_file():
        comparison = json.loads(serving_path.read_text(encoding="utf-8"))
        phases = {"reference": comparison.get("reference", {})}
        phases.update({row["label"]: row for row in comparison.get("candidates", [])})
    for phase, snapshot in phases.items():
        services = []
        for service in snapshot.get("services", []):
            spec = service["spec"]
            service_settings = {key: spec[key] for key in (
                "model", "backend", "engineArgs", "env", "resources", "replicas", "nodes", "autoscaling",
            ) if key in spec}
            if "modelPools" in spec:
                service_settings["modelPoolNames"] = sorted(pool["name"] for pool in spec["modelPools"])
                service_settings["modelPools"] = {pool["name"]: {key: pool[key] for key in (
                    "role", "engineArgs", "env", "resources", "replicas", "nodes",
                ) if key in pool} for pool in spec["modelPools"]}
            services.append(service_settings)
        groups = [{
            "artifacts": group.get("artifacts"), "resources": group.get("resources"),
            "parallelism": group.get("parallelism"),
            "runtime": {key: group["runtime"][key] for key in (
                "backend", "engineArgs", "env", "image", "applicationURL",
            ) if key in group["runtime"]},
        } for group in snapshot.get("groups", [])]
        # Keep code selections observed during a rollout, without comparing disposable Pod identities.
        applications = [controller["application"] for controller in snapshot.get("controllers", [])]
        applications.extend({"image": container["image"], "applicationURL": pod["application_url"]}
                            for pod in snapshot.get("pods", []) if "application_url" in pod
                            for container in pod.get("containers", []) if container["name"] == "manager")
        controllers = {json.dumps(application, sort_keys=True): application for application in applications}
        frontends = []
        for frontend in snapshot.get("frontends", []):
            application = frontend.get("application") or {}
            frontends.append({
                "application": {key: application[key] for key in ("image", "applicationURL") if key in application} or None,
                "spec": {key: frontend["spec"][key] for key in (
                    "replicas", "resources", "routerPipeline", "admission", "roleRules",
                ) if key in frontend["spec"]},
            })
        settings[phase] = {
            "source": snapshot.get("source"),
            "declared_gpu_count": snapshot.get("declared_gpu_count"),
            "controllers": [controllers[key] for key in sorted(controllers)] or None,
            "frontends": sorted(frontends, key=lambda value: json.dumps(value, sort_keys=True)) or None,
            "services": sorted(services, key=lambda value: json.dumps(value, sort_keys=True)) or None,
            "groups": sorted(groups, key=lambda value: json.dumps(value, sort_keys=True)) or None,
        }
    return settings


def _comparison_changes(records: list[dict[str, Any]]) -> None:
    """Add only differing recorded settings to run-level CSV records, leaving scores and summaries intact."""
    def fields(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict) and value:
            return {field: item for key, child in value.items()
                    for field, item in fields(child, f"{prefix}.{key}" if prefix else key).items()}
        if isinstance(value, list) and value:
            return {field: item for index, child in enumerate(value)
                    for field, item in fields(child, f"{prefix}.{index + 1}").items()}
        return {prefix: value}

    for section in ("conditions", "environment_settings"):
        values = []
        for record in records:
            value = record.get(section)
            if section == "conditions" and value is not None and value.get("mode") == "evaluation" and value.get("evaluation") is None:
                value = None
            values.append(fields(value) if value is not None else None)
        observed = [value for value in values if value is not None]
        keys = {key for value in observed for key in value}
        changed = {key for key in keys if len({
            (key in value, json.dumps(value.get(key), sort_keys=True)) for value in observed
        }) > 1}
        output = "conditions_changes" if section == "conditions" else "environment_changes"
        for record, value in zip(records, values):
            record[output] = {key: value[key] for key in sorted(changed) if value is not None and key in value}


def _leaf(
    directory: Path, configuration: Path | None, conditions: dict[str, Any], method: str,
    exit_code: int | None, started: bool, status: str | None = None,
) -> _RecordedRun:
    """Read one measurement, retaining the parent's plan when execution never reached it."""
    task_path = directory / "task.json"
    if task_path.is_file():
        task = json.loads(task_path.read_text())
        exit_code, status, started = task["exit_code"], task["status"], True
        if status == "completed" and exit_code:
            status = "failed"
    source = task_source(directory)
    if (source / "config.json").is_file():
        configuration = source / "config.json"
        conditions = _conditions(json.loads(configuration.read_text()))
        started = True
    metrics_path = source / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else None
    context_path = source / "context.json"
    context = json.loads(context_path.read_text()) if context_path.is_file() else None
    if context is not None:
        exit_code = context["measurement_exit_code"]
        started = True
    elif status in {"failed", "interrupted", "running", "pending"}:
        exit_code = None
    return _RecordedRun(directory, configuration, conditions, method, metrics, exit_code, started, status, context)


def _recorded_runs(directory: Path) -> Iterator[_RecordedRun]:
    """Follow the saved sweep/evaluation plan rather than rediscovering nested native reports."""
    configuration = task_source(directory) / "config.json"
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
            yield _RecordedRun(directory, probe, conditions, "default", None, None, True)
        elif (directory / "context.json").is_file():
            yield _leaf(directory, None, {}, "default", None, True)
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
                            point["exit_code"] if point is not None else None,
                            point is not None and point.get("status") != "pending",
                            point.get("status") if point is not None else None)
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
    yield _leaf(directory, configuration, _conditions(config), _method({"bench": config}),
                None, True)


def _parent_plan(directory: Path) -> tuple[Path, str] | None:
    """Locate an enclosing result plan without crossing its command boundary."""
    for parent in directory.parents:
        if (parent / "generated/context.json").is_file():
            break
        configuration = parent / "config.json"
        config = json.loads(configuration.read_text()) if configuration.is_file() else {}
        if config.get("mode") in {"parameter_sweep", "video_parameter_sweep"}:
            return parent, "sweep"
        metrics_path = parent / "metrics.json"
        metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
        if "evaluation_comparison" in metrics:
            return parent, "evaluation"
        if config.get("mode") == "slo_search" or (parent / "slo_results.json").is_file():
            return parent, "slo_search"
    return None


def _selected_runs(
    directory: Path, plans: dict[Path, list[_RecordedRun]],
) -> Iterator[_RecordedRun]:
    """Restore selected children's methods and exit states from their owning plan."""
    parent = _parent_plan(directory)
    if parent is not None:
        owner, kind = parent
        if kind != "slo_search":
            if owner not in plans:
                plans[owner] = list(_recorded_runs(owner))
            selected = [run for run in plans[owner] if run.directory.is_relative_to(directory)]
            if not selected:
                raise FileNotFoundError(f"No planned benchmark results in {directory}")
            yield from selected
            return
    yield from _recorded_runs(directory)


@dataclass(frozen=True)
class _ResultDirectory:
    """A saved result set and the optional command record that owns its execution."""

    directory: Path
    group: Path
    context: Path | None


def _enclosing_context(directory: Path) -> Path | None:
    """Find a command record when a user selects one of its result directories directly."""
    return next((path for parent in (directory, *directory.parents)
                 if (path := parent / "generated/context.json").is_file()), None)


def _discover(
    directory: Path, visited: set[Path], *, group: Path | None = None,
    context: Path | None = None,
) -> Iterator[_ResultDirectory]:
    """Descend directory containers, stopping at result owners before their native children."""
    directory = directory.resolve()
    group = group.resolve() if group is not None else None
    if directory in visited:
        return
    visited.add(directory)
    command = directory / "generated/context.json"
    if command.is_file():
        found = False
        for child in sorted((directory / "artifacts").glob("*")):
            if child.is_dir():
                for result in _discover(child, visited, group=group or directory, context=command):
                    found = True
                    yield result
        if not found:
            yield _ResultDirectory(directory, group or directory, command)
        return
    if any((directory / name).is_file() for name in ("config.json", "slo_results.json", "task.json", "context.json")):
        yield _ResultDirectory(directory, group or directory, context or _enclosing_context(directory))
        return
    if (directory / "iterations").is_dir():
        for child in sorted((directory / "iterations").iterdir()):
            if child.is_dir():
                yield from _discover(child, visited, group=child)
        return
    if (directory / "runs").is_dir():
        for child in sorted((directory / "runs").iterdir()):
            if child.is_dir():
                yield from _discover(child, visited, group=group or directory)
        return
    parent = _parent_plan(directory)
    if parent is not None and parent[1] != "slo_search":
        # A selected sweep point still owns its planned repetitions, including
        # children that have not created their directories yet.
        yield _ResultDirectory(directory, group or directory, context or _enclosing_context(directory))
        return
    for child in sorted(directory.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield from _discover(child, visited, group=group, context=context)


def _directory_labels(paths: list[Path]) -> dict[Path, str]:
    """Use the shortest distinct directory suffix so equal iteration names remain distinguishable."""
    labels = {}
    for path in paths:
        for width in range(1, len(path.parts) + 1):
            suffix = path.parts[-width:]
            if all(other == path or other.parts[-width:] != suffix for other in paths):
                labels[path] = Path(*suffix).as_posix()
                break
    return labels


def read_saved_results(
    sources: tuple[Path, ...], *, methods: tuple[str, ...] = (), base: Path | None = None,
) -> SavedResults:
    """Read directory selections once and group measurements by their recorded workloads.

    Experiment iterations and declared sweep repetitions retain their statistical
    identity. Standalone result sets remain separate approaches. Native scores keep
    evaluator errors, and incomplete command records remain visible without metrics.
    An optional base directory is included once and retains its own repetition group
    when selected inside a broader candidate directory.
    """
    base = base.resolve() if base is not None else None
    selected = list(dict.fromkeys(path.resolve() for path in ((base, *sources) if base is not None else sources)))
    for path in selected:
        if not path.is_dir():
            raise NotADirectoryError(f"Result directory does not exist: {path}")
    # Keep an explicit base first so it owns shared resumed measurements. Other
    # enclosing selections own traversal; leaf evidence is still counted only once.
    selected = [path for path in selected if path == base or not any(
        path != parent and path.is_relative_to(parent) for parent in selected
    )]
    directories: list[_ResultDirectory] = []
    for source in selected:
        discovered = list(_discover(source, set()))
        if not discovered:
            raise FileNotFoundError(f"No saved benchmark results in {source}")
        directories.extend(discovered)
    labels = _directory_labels(list(dict.fromkeys(item.group for item in directories)))
    base_approaches: set[str] = set()
    runs: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    native: dict[tuple[Any, ...], Chart] = {}
    combinations: dict[tuple[str, Path | None], str] = {}
    repetition_counts: dict[tuple[str, str], dict[str, int]] = {}
    available_methods: set[str] = set()
    method_filters: dict[Path, tuple[str, ...]] = {}
    observed: set[Path] = set()
    plans: dict[Path, list[_RecordedRun]] = {}
    for source in directories:
        context = json.loads(source.context.read_text()) if source.context is not None else {}
        command = source.context.parent.parent if source.context is not None else None
        iteration = command.parent.parent if command is not None and command.parent.name == "runs" else None
        identity = {
            "source": str(source.directory), "label": labels[source.group],
            "iteration": iteration.name if iteration is not None else None,
            "run": command.name if command is not None else source.directory.name,
            "status": context.get("status"), "exit_code": context.get("exit_code"),
            "command": context.get("command"),
            "context": str(source.context) if source.context is not None else None,
        }
        found = False
        for saved in _selected_runs(source.directory, plans):
            found = True
            task_path = saved.directory.resolve()
            result_path = task_source(task_path)
            saved = replace(saved, directory=result_path)
            if result_path in observed:
                continue
            observed.add(result_path)
            measured = saved.metrics or {}
            quality = saved.conditions.get("mode") == "evaluation"
            if quality:
                from benchmarks.results.evaluation import read_quality_conditions

                saved.conditions["evaluation"] = read_quality_conditions(
                    saved.conditions["evaluator"], saved.directory / "native",
                )
            for kind in ("distribution_comparison", "greedy_comparison"):
                if kind in measured:
                    saved.conditions["protocol"] = measured[kind].get("protocol")
                    saved.conditions["reference_model"] = measured[kind].get("reference_model")
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
            if comparison is not None and methods:
                method_filters[saved.directory] = tuple(chosen)
            is_base = base is not None and task_path.is_relative_to(base)
            group = source.group
            label = labels[group] + (f" / {saved.method}" if saved.method != "default" else "")
            if is_base:
                base_approaches.add(label)
            # Missing workload configuration is unknown, not equal to another run's.
            fixed = (json.dumps(saved.conditions, sort_keys=True),
                     saved.directory if saved.configuration is None or quality and saved.conditions["evaluation"] is None else None)
            combination = combinations.setdefault(fixed, f"workload-{len(combinations) + 1}")
            lifecycle = saved.context or {}
            runs.append({**identity,
                         "status": lifecycle.get("status") if lifecycle.get("finished_at") else None,
                         "exit_code": lifecycle.get("exit_code") if lifecycle.get("finished_at") else None,
                         "phase": lifecycle.get("phase"),
                         "error_type": lifecycle.get("error_type"),
                         "context": str(saved.directory / "context.json") if saved.context is not None else identity["context"],
                         "label": labels[group], "method": saved.method, "methods": chosen,
                         "approach": label, "workload": combination,
                         "result": str(saved.directory), "measured": saved.exit_code is not None or saved.metrics is not None or bool(charts),
                         "started": saved.started, "measurement_exit_code": saved.exit_code,
                         "task": str(task_path), "task_status": saved.status,
                         "config": str(saved.configuration) if saved.configuration is not None else None,
                         "conditions": saved.conditions,
                         "environment": next((str(path) for name in ("environment.json", "serving_environments.json")
                                              if (path := saved.directory / name).is_file()), None),
                         "environment_settings": _environment_settings(saved.directory),
                         **{key: measured.get(key) for key in (
                             "planned_num", "admitted_num", "request_num", "success_num", "failed_num",
                             "not_executed_num", "not_executed_reasons", "failure_reasons",
                         )}})
            if charts:
                for chart in charts:
                    series = []
                    for item in chart.series:
                        method = str(item.records[0].get("method") or item.name) if comparison is not None else saved.method
                        if method not in chosen:
                            continue
                        method_label = labels[group] + (f" / {method}" if method != "default" else "")
                        name = method_label
                        if command is not None and group != command:
                            name += f" / {command.name}"
                        if comparison is not None and item.name != method:
                            name += f" / {item.name}"
                        series.append(replace(item, name=name, records=tuple(
                            {**row, "method": method_label, "result": str(saved.directory)}
                            for row in item.records
                        )))
                    if not series:
                        continue
                    key = (chart.name, chart.title, chart.ylabel, chart.tick_labels, fixed)
                    if key in native:
                        native[key] = replace(native[key], series=native[key].series + tuple(series))
                    else:
                        native[key] = replace(chart, name=f"comparison-{combination}-{chart.name}", series=tuple(series))
            else:
                counts = repetition_counts.setdefault((label, combination), {"runs": 0, "requested_runs": 0, "failed_runs": 0})
                counts["runs"] += int(saved.started)
                counts["requested_runs"] += 1
                counts["failed_runs"] += int(bool(saved.exit_code) or bool(measured.get("failed_num")))
                if saved.metrics is not None:
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

    _comparison_changes(runs)
    summary = summarize_sweep(points)
    for row in summary:
        row.update(repetition_counts[row["method"], row["combination"]])
    return SavedResults(runs, points, summary, list(native.values()), method_filters, tuple(sorted(base_approaches)))
