# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Project iteration-owned judgments and saved runs into RSI tables and charts."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import Field, ValidationError, model_validator

from benchmarks.results.measurements.sweeps import summarize_sweep
from benchmarks.results.plots.data import _metric_display
from benchmarks.results.plots.progress import _Progress, _ProgressRecord, render_progress
from benchmarks.results.plots.rendering import FigureWriter
from benchmarks.results.plots.saved_results import _comparison_changes, read_saved_results
from benchmarks.results.recording.database import read_records


class _Measurement(_ProgressRecord):
    """One owner's selected repetitions and judgment for a comparison scope."""

    runs: list[str] = Field(default_factory=list)
    status: Literal["valid", "reference", "incorrect", "compile_failed", "runtime_failed"]
    retained: bool = False
    parent: str | None = None

    @model_validator(mode="after")
    def retained_result(self) -> _Measurement:
        """Keep retention separate from failures and unresolved output questions."""
        if not self.runs and self.status in {"valid", "reference"}:
            raise ValueError("Measured and reference judgments need saved runs")
        if self.retained and self.status != "valid":
            raise ValueError("A retained measurement must have status: valid")
        return self


class _Iteration(_ProgressRecord):
    """The description an iteration owner adds to its existing result directory."""

    label: str = Field(min_length=1)
    baseline: bool = False
    measurements: dict[str, _Measurement]


def _link(path: Path, directory: Path) -> str:
    return quote(Path(os.path.relpath(path, directory)).as_posix(), safe="/.")


def _read_measurement(paths: tuple[Path, ...], metrics: tuple[str, ...]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reuse saved-result discovery and repetition statistics for one declared scope."""
    saved = read_saved_results(paths, derive_metrics=True, derived_metrics=metrics)
    workloads = {json.dumps(run["conditions"], sort_keys=True) for run in saved.runs if "conditions" in run}
    if len(workloads) > 1:
        raise ValueError(f"Progress runs contain different approaches or workloads: {paths}; select one scope's result directories")
    points = [{**point, "combination": "selected",
               "bench": {**point["bench"], "service": {"name": "selected"}}} for point in saved.points]
    summaries = {row["metric"]: row for row in summarize_sweep(points)}
    return summaries, saved.runs


def _matching_workloads(entries: list[dict[str, Any]], scope: str) -> None:
    """Keep known workload differences out of one scope, without equating missing evidence."""
    evidence = [dict(run) for entry in entries for run in entry["evidence"]]
    _comparison_changes(evidence)
    values: dict[str, list[Any]] = defaultdict(list)
    for run in evidence:
        for field, value in run["conditions_changes"].items():
            values[field].append(value)
    changed = [field for field, observed in values.items() if any(value != observed[0] for value in observed[1:])]
    if changed:
        raise ValueError(f"Progress scope {scope} has different recorded workloads: {', '.join(changed)}; separate comparison scopes")


def _retained_tips(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find measured adopted branch endpoints without ranking or combining siblings."""
    parents = {index: [other for other, candidate in enumerate(entries)
                       if entry["parent_runs"] and set(candidate["runs"]) == set(entry["parent_runs"])]
               for index, entry in enumerate(entries)}
    superseded = set()
    for index, entry in enumerate(entries):
        if not entry["retained"]:
            continue
        pending, visited = list(parents[index]), {index}
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            superseded.add(parent)
            pending.extend(parents[parent])
    return [entry for index, entry in enumerate(entries) if entry["retained"] and index not in superseded]


def _tip_summary(entries: list[dict[str, Any]], directory: Path) -> list[str]:
    """Link adopted branch tips to their actual runs and available configuration/source records."""
    lines = ["### Measured retained branch tips", ""]
    tips = _retained_tips(entries)
    if not tips:
        message = "No retained branch endpoint can be resolved from these records." if any(entry["retained"] for entry in entries) else "No retained candidate is recorded for this scope."
        return [*lines, message, ""]
    for entry in tips:
        links = []
        for run in entry["evidence"]:
            if not run.get("result") or run["result"] not in entry["runs"]:
                continue
            links.append(f"[run]({_link(Path(run['result']), directory)})")
            for field, label in (("config", "configuration"), ("context", "command/client source"), ("environment", "environment")):
                if run.get(field):
                    links.append(f"[{label}]({_link(Path(run[field]), directory)})")
        if entry["parent"]:
            links.append(f"[measured parent]({_link(Path(entry['parent']), directory)})")
        lines.append(f"- [{entry['label']}]({entry['source']}): " + " · ".join(dict.fromkeys(links)))
    return [*lines, ""]


def render_iteration_progress(root: Path, writer: FigureWriter, metrics: tuple[str, ...]) -> None:
    """Read all owner records and publish matching views while the exporter holds its lock.

    Judgments are owned by each iteration. Metrics and provenance remain in the
    existing run records; this view never selects or combines adopted versions.
    """
    metrics = metrics or ("generation_tokens_per_second", "tpot_mean_seconds")
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    provenance = []
    for path in sorted(root.glob("iterations/*/notes/progress.yaml")):
        try:
            iteration = _Iteration.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as error:
            raise ValueError(f"Invalid iteration progress file {path}: {error}") from error
        for scope, measurement in iteration.measurements.items():
            sources = tuple((path.parent / name).resolve() for name in measurement.runs)
            summary, runs = _read_measurement(sources, metrics)
            parent = (path.parent / measurement.parent).resolve() if measurement.parent else None
            parent_summary, parent_runs = _read_measurement((parent,), metrics) if parent else ({}, [])
            if measurement.retained and not iteration.baseline and parent is None:
                raise ValueError(f"Retained candidate {path} ({scope}) needs its measured parent run")
            finishes = []
            for run in runs:
                context_path = Path(run["context"]) if run.get("context") else None
                context = read_records(context_path.parent, (context_path.name,)).get(context_path.name, {}) if context_path else {}
                if context.get("finished_at"):
                    finishes.append(context["finished_at"])
                provenance.append({"scope": scope, "iteration": path.parent.parent.name, **run,
                                   "client_checkout_revision": context.get("checkout", {}).get("commit"), "parent": str(parent) if parent else None})
            records[scope].append({"iteration": path.parent.parent.name, "label": iteration.label,
                                   "baseline": iteration.baseline, "status": measurement.status,
                                   "retained": measurement.retained, "summary": summary,
                                   "source": _link(path.parent / "iteration.md", writer.directory),
                                   "runs": [run.get("result") for run in runs],
                                   "parent": str(parent) if parent else None,
                                   "parent_runs": [run.get("result") for run in parent_runs],
                                   "parent_summary": parent_summary, "evidence": [*runs, *parent_runs],
                                   "finished_at": max(finishes, default="")})
    if not records:
        raise ValueError(f"No iteration progress records in {root}/iterations/*/notes/progress.yaml")

    updated = datetime.now(UTC).isoformat()
    markdown = ["# Optimization results", "", f"Updated: {updated}", "",
                "Values are means of the selected saved runs. Retention applies only to the named scope and measured configuration.", "",
                "[Measurements and judgments](progress-summary.csv) · [Run, configuration and source evidence](progress-runs.csv)", ""]
    rows = []
    for scope, entries in records.items():
        _matching_workloads(entries, scope)
        baselines = [entry for entry in entries if entry["baseline"]]
        if len(baselines) > 1:
            raise ValueError(f"Multiple original baseline records in scope {scope}")
        baseline = baselines[0] if baselines else None
        entries.sort(key=lambda entry: (not entry["baseline"], entry["finished_at"], entry["iteration"]))
        markdown.extend([f"## {scope}", "", *_tip_summary(entries, writer.directory)])
        for metric in metrics:
            scale, label = _metric_display(metric)
            direction = "lower" if metric.endswith("_seconds") else "higher"
            stem = f"progress-{quote(scope, safe='')}--{quote(metric, safe='')}"
            baseline_value = baseline["summary"].get(metric, {}).get("mean") if baseline else None
            data = {"title": f"{scope} · {label}", "updated_at": updated, "x_label": "Measured approach",
                    "metric": {"name": label, "unit": "", "direction": direction},
                    "baseline": {"label": baseline["label"] if baseline else "Initial baseline (unmeasured)",
                                 "value": baseline_value * scale if baseline_value is not None else None,
                                 "source": baseline["source"] if baseline else ""},
                    "points": [], "references": []}
            indexed = list(enumerate(entries, start=0 if baseline else 1))
            for index, entry in indexed:
                summary = entry["summary"].get(metric, {})
                value = summary.get("mean")
                parent_value = entry["parent_summary"].get(metric, {}).get("mean")
                rows.append({"scope": scope, "iteration": entry["iteration"], "label": entry["label"],
                             "metric": metric, "value": value, "samples": summary.get("samples", 0),
                             "stddev": summary.get("stddev"), "status": entry["status"],
                             "retained": entry["retained"], "baseline": entry["baseline"],
                             "baseline_value": baseline_value,
                             "baseline_change_percent": (value / baseline_value - 1) * 100 if value is not None and baseline_value else None,
                             "parent_value": parent_value,
                             "parent_change_percent": (value / parent_value - 1) * 100 if value is not None and parent_value else None,
                             "runs": entry["runs"], "parent_runs": entry["parent_runs"], "notes": entry["source"]})
                if entry["baseline"]:
                    continue
                point = {"x": float(index), "value": value * scale if value is not None else None,
                         "source": entry["source"], "label": entry["label"]}
                if entry["status"] == "valid":
                    if value is None:
                        raise ValueError(f"Valid progress measurement {entry['iteration']} ({scope}) has no {metric}")
                    parent_index = next((i for i, other in indexed if entry["parent_runs"]
                                         and set(other["runs"]) == set(entry["parent_runs"])), None)
                    data["points"].append({**point, "status": "valid", "checkpoint": entry["retained"],
                                           "parent_x": float(parent_index) if parent_index is not None else None})
                elif entry["status"] != "reference":
                    data["points"].append({**point, "value": None, "status": entry["status"]})
                if value is not None and entry["status"] != "valid":
                    data["references"].append({"label": f"{entry['label']} ({entry['status']})", "source": entry["source"],
                                               "points": [{"x": float(index), "value": value * scale}]})
            render_progress(_Progress.model_validate(data).model_dump(exclude_unset=True),
                            writer.directory / f"{stem}.yaml", writer)
            markdown.extend([f"![{label}: retained versions]({_link(writer.directory / (stem + '-milestones.png'), writer.directory)})", "",
                             f"![{label}: search history]({_link(writer.directory / (stem + '-search.png'), writer.directory)})", ""])
        markdown.extend(["| Approach | " + " | ".join(_metric_display(metric)[1] for metric in metrics) + " | Measured parent | Judgment |",
                         "| --- | " + " | ".join("---:" for _ in metrics) + " | --- | --- |"])
        for entry in entries:
            cells = []
            for metric in metrics:
                summary = entry["summary"].get(metric, {})
                value = summary.get("mean")
                base = baseline["summary"].get(metric, {}).get("mean") if baseline else None
                cell = f"{value * _metric_display(metric)[0]:.4g}" if value is not None else "—"
                parent_value = entry["parent_summary"].get(metric, {}).get("mean")
                changes = []
                if value is not None and parent_value:
                    changes.append(f"parent {(value / parent_value - 1) * 100:+.2f}%")
                if value is not None and base:
                    changes.append(f"initial {(value / base - 1) * 100:+.2f}%")
                if changes:
                    cell += " (" + "; ".join(changes) + ")"
                if summary.get("samples", 0) > 1:
                    if summary.get("stddev") is not None:
                        cell += f"; SD={summary['stddev'] * _metric_display(metric)[0]:.3g}"
                    cell += f"; n={summary['samples']}"
                cells.append(cell)
            judgment = entry["status"] + ("; retained" if entry["retained"] else "")
            parent_link = f"[run]({_link(Path(entry['parent']), writer.directory)})" if entry["parent"] else "—"
            markdown.append(f"| [{entry['label']}]({entry['source']}) | " + " | ".join(cells) + f" | {parent_link} | {judgment} |")
        markdown.append("")
    writer.table("progress-summary", rows)
    writer.table("progress-runs", provenance)
    path = writer.directory / "progress-summary.md"
    writer.files[path.name] = path
    path.write_text("\n".join(markdown) + "\n", encoding="utf-8")
