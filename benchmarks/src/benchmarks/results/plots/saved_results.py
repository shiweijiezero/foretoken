# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read saved result directories and their execution plans without running benchmarks."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.results.plots.charts.quality import _quality_charts, comparison_charts
from benchmarks.results.plots.charts.slo import _slo_charts
from benchmarks.results.plots.data import Chart, _method
from benchmarks.results.recording.database import read_record, read_records
from benchmarks.results.recording.directories import (
    _parent_plan,
    discover_result_directories,
)
from benchmarks.results.recording.tasks import task_source

if TYPE_CHECKING:
    from benchmarks.results.plots.economics import Pricing


@dataclass
class SavedResults:
    """Run evidence and comparison inputs consumed by the figure exporter."""

    runs: list[dict[str, Any]]
    points: list[dict[str, Any]]
    summary: list[dict[str, Any]]
    native: list[Chart]
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
    parameter_group: str | None = None
    combination: str | None = None
    run_number: int | None = None
    combination_identity: tuple[Path | None, str] | None = None


def read_saved_results(
    sources: tuple[Path, ...], *, methods: tuple[str, ...] = (), base: Path | None = None,
    derive_metrics: bool = False, gpu_count: int | None = None, pricing: Pricing | None = None,
    derived_metrics: Collection[str] | None = None,
) -> SavedResults:
    """Read directory selections once and group measurements by their recorded workloads.

    Experiment iterations and declared sweep repetitions retain their statistical
    identity. Standalone result sets remain separate approaches. Native scores keep
    evaluator errors, and incomplete command records remain visible without metrics.
    An optional base directory is included once and retains its own repetition group
    when selected inside a broader candidate directory. Method selections accept
    displayed approach names and recorded service names; the explicit base is
    retained independently of candidate selections.
    Missing legacy output-token totals are recovered from complete request records;
    ``derive_metrics`` controls additional chart metrics.
    """
    base = base.resolve() if base is not None else None
    directories = discover_result_directories(sources, base=base)
    labels = _directory_labels(list(dict.fromkeys(item.group for item in directories)))
    base_approaches: set[str] = set()
    runs: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    native: dict[tuple[Any, ...], Chart] = {}
    combinations: dict[tuple[str, str, tuple[Path | None, str] | None, Path | None], str] = {}
    repetition_counts: dict[tuple[str, str], dict[str, int]] = {}
    available_methods: set[str] = set()
    observed: set[Path] = set()
    plans: dict[Path, list[_RecordedRun]] = {}
    for source in directories:
        context = read_record(source.context.parent, source.context.name) if source.context is not None else {}
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
                if saved.conditions["evaluator"] == "lm-eval":
                    from benchmarks.integrations.lm_eval.reports import read_conditions
                else:
                    from benchmarks.integrations.evalscope.reports import (
                        read_conditions,
                    )

                saved.conditions["evaluation"] = read_conditions(saved.directory / "native")
            for kind in ("distribution_comparison", "greedy_comparison"):
                if kind in measured:
                    saved.conditions["protocol"] = measured[kind].get("protocol")
                    saved.conditions["reference_model"] = measured[kind].get("reference_model")
            comparison = comparison_charts(measured)
            search = read_records(saved.directory, ("slo_results.json",)).get("slo_results.json")
            charts = []
            if comparison is not None:
                charts, identities = comparison
            elif "scores" in measured:
                charts, identities = _quality_charts(measured), [saved.method]
            elif search is not None:
                charts = _slo_charts(search)
                identities = [saved.method]
            else:
                identities = [saved.method]
            group = source.group
            display_methods = {
                method: (method if len(labels) == 1 and method != "default" else
                         labels[group] + (f" / {method}" if method != "default" else ""))
                for method in dict.fromkeys((saved.method, *identities))
            }
            available_methods.update(identities)
            available_methods.update(display_methods[method] for method in identities)
            is_base = base is not None and task_path.is_relative_to(base)
            chosen = [method for method in identities
                      if is_base or not methods or method in methods or display_methods[method] in methods]
            if not chosen:
                continue
            label = display_methods[saved.method]
            if "throughput" in measured:
                from benchmarks.results.plots.metrics import enrich_plot_metrics

                selected = None
                if not derive_metrics:
                    selected = {"total_output_tokens"}
                elif derived_metrics is not None:
                    selected = set(derived_metrics) | {"total_output_tokens"}
                measured = enrich_plot_metrics(
                    saved.directory, measured, gpu_count=gpu_count if derive_metrics else None,
                    selected=selected,
                )
                if derive_metrics and pricing is not None:
                    from benchmarks.results.plots.economics import price_metrics

                    measured.update(price_metrics(measured, label, pricing))
            if is_base:
                base_approaches.add(label)
            # Missing workload configuration is unknown, not equal to another run's.
            parameter_group = saved.parameter_group if saved.parameter_group is not None else str(saved.conditions.get("mode", "performance"))
            fixed = (parameter_group, json.dumps(saved.conditions, sort_keys=True), saved.combination_identity,
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
                         "parameter_group": parameter_group,
                         "source_combination": saved.combination, "run_number": saved.run_number,
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
                        method_label = display_methods[method]
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
                                   "parameter_group": parameter_group,
                                   "bench": {**saved.conditions, "service": {"name": label}},
                                   "source_combination": saved.combination, "run_number": saved.run_number,
                                   "exit_code": saved.exit_code})
        if not found and not methods:
            runs.append({**identity, "result": None, "measured": False})
    unknown = set(methods) - available_methods
    if unknown:
        raise ValueError("Unknown plot methods: " + ", ".join(sorted(unknown)))
    from benchmarks.results.measurements.sweeps import summarize_sweep

    _comparison_changes(runs)
    summary = summarize_sweep(points)
    for row in summary:
        row.update(repetition_counts[row["method"], row["combination"]])
    return SavedResults(runs, points, summary, list(native.values()), tuple(sorted(base_approaches)))


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
    task = read_records(directory, ("task.json",)).get("task.json")
    if task is not None:
        exit_code, status, started = task["exit_code"], task["status"], True
        if status == "completed" and exit_code:
            status = "failed"
    source = task_source(directory)
    documents = read_records(source, ("config.json", "metrics.json", "context.json"))
    if "config.json" in documents:
        configuration = source / "config.json"
        conditions = _conditions(documents["config.json"])
        started = True
    metrics = documents.get("metrics.json")
    context = documents.get("context.json")
    if context is not None:
        exit_code = context["measurement_exit_code"]
        started = True
    elif status in {"failed", "interrupted", "running", "pending"}:
        exit_code = None
    return _RecordedRun(directory, configuration, conditions, method, metrics, exit_code, started, status, context)


def _recorded_runs(directory: Path) -> Iterator[_RecordedRun]:
    """Follow the saved sweep/evaluation plan rather than rediscovering nested native reports."""
    source = task_source(directory)
    configuration = source / "config.json"
    documents = read_records(source, ("config.json", "metrics.json", "context.json", "slo_results.json", "sweep_points.json"))
    if "config.json" not in documents:
        if "slo_results.json" in documents:
            # Older search summaries retain their fixed workload only in probe records.
            probe = None
            conditions = {}
            for child in sorted(directory.glob("group-*/max-concurrency-*/run-*")):
                saved = read_records(child, ("config.json",)).get("config.json")
                if saved is not None:
                    probe, conditions = child / "config.json", _conditions(saved)
                    break
            for field in ("max_concurrency", "slo_params", "slo_search"):
                conditions.pop(field, None)
            search = documents["slo_results.json"]
            conditions.update(
                mode="slo_search", concurrency_limit_unit=search["concurrency_limit_unit"],
                slo_params=[group["criteria"] for group in search["groups"]],
            )
            yield _RecordedRun(directory, probe, conditions, "default", None, None, True)
        elif "context.json" in documents:
            yield _leaf(directory, None, {}, "default", None, True)
        return
    config = documents["config.json"]
    if config.get("mode") in {"parameter_sweep", "video_parameter_sweep"}:
        points = documents.get("sweep_points.json", [])
        observed = {(point["combination"], point["run_number"]): point for point in points}
        retained_runs: list[_RecordedRun] = []
        point_identities: dict[tuple[str, str, str], set[str]] = {}
        declared_names: dict[str, str | None] = {}
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
            configurations = (
                read_records(task_source(child), ("config.json",)).get("config.json")
                for child in children
            )
            saved = next((value for value in configurations if value is not None), None)
            if saved is not None:
                planned = _conditions(saved)
            group = combination["bench"].get("_parameter_group")
            if group is None:
                group = next((point["parameter_group"] for point in points
                              if point["combination"] == combination["dir"] and "parameter_group" in point),
                             "default")
            group = str(group)
            declared_names[str(combination["dir"])] = combination["bench"].get("_benchmark_name")
            for number, child in enumerate(children):
                point = observed.get((combination["dir"], number))
                run = _leaf(child, configuration, planned, method,
                            point.get("exit_code") if point is not None else None,
                            point is not None and point.get("status") != "pending",
                            point.get("status") if point is not None else None)
                if run.metrics is None and point is not None:
                    # The aggregate retains each actual run, even when its child
                    # artifacts were not kept. Summary means are never samples.
                    retained = {key: value for key, value in point.items() if key not in {
                        "combination", "parameter_group", "run_number", "bench", "label", "status",
                        "exit_code", "result", "reused", "error_type",
                    }}
                    if retained:
                        run = replace(run, metrics=retained, exit_code=point.get("exit_code"), started=True)
                run = replace(
                    run, parameter_group=str(point.get("parameter_group", group)) if point is not None else group,
                    combination=str(combination["dir"]), run_number=number,
                )
                retained_runs.append(run)
                identity = (run.method, run.parameter_group, json.dumps(run.conditions, sort_keys=True))
                point_identities.setdefault(identity, set()).add(run.combination)
        for run in retained_runs:
            identity = (run.method, run.parameter_group, json.dumps(run.conditions, sort_keys=True))
            if len(point_identities[identity]) > 1:
                # Distinct declared points with the same conditions remain separate.
                # An explicit shared name can match them across approaches; an
                # unnamed collision remains local to its source plan.
                name = declared_names[run.combination]
                run = replace(run, combination_identity=(None, str(name)) if name is not None else
                              (directory.resolve(), run.combination))
            yield run
        return
    metrics = documents.get("metrics.json")
    if metrics is not None and "evaluation_comparison" in metrics:
        # The comparison owns method labels and exit states; child reports own
        # native scores and environment snapshots. Never count the aggregate twice.
        for method in metrics["evaluation_comparison"]["methods"]:
            yield _leaf(directory / method["directory"], configuration, _conditions(config),
                        method["label"], method["exit_code"], method["status"] != "not_started")
        return
    yield _leaf(directory, configuration, _conditions(config), _method({"bench": config}),
                None, True)


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
