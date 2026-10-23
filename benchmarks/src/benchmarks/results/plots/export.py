# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export saved result views and publish their files and ownership under one directory lock."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from benchmarks.results.plots.charts.http import _http_charts
from benchmarks.results.plots.charts.phases import phase_summary_charts
from benchmarks.results.plots.charts.quality import _quality_charts, comparison_charts
from benchmarks.results.plots.charts.resources import (
    _prometheus_charts,
    gpu_allocation_charts,
)
from benchmarks.results.plots.charts.slo import _slo_charts
from benchmarks.results.plots.charts.video import _video_phase_charts
from benchmarks.results.plots.comparison import compare_to_base
from benchmarks.results.plots.data import Chart, _method, sweep_charts
from benchmarks.results.plots.rendering import _charts, _pareto, _table
from benchmarks.results.plots.saved_results import SavedResults, read_saved_results
from benchmarks.results.recording.database import read_record, read_records
from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.tasks import task_source


@dataclass(frozen=True)
class FigureExport:
    """Files and optional single-run owner captured together by the figure exporter."""

    files: dict[str, Path]
    source: Path | None


def render_results(
    source: Path,
    *others: Path,
    output_dir: Path | None = None,
    columns: int = 1,
    metrics: tuple[str, ...] = (),
    methods: tuple[str, ...] = (),
    base: Path | None = None,
) -> FigureExport:
    """Export figures under an output-directory lock shared with W&B artifact staging."""
    for selected in (source, *others, *((base,) if base is not None else ())):
        if not Path(selected).is_dir():
            raise NotADirectoryError(f"Result directory does not exist: {selected}")
    out = Path(output_dir) if output_dir is not None else default_output_directory(source, *others, base=base)
    out.mkdir(parents=True, exist_ok=True)
    with publication_lock(out, scope="plots"):
        # A failed redraw must not attribute partially replaced files to the previous export.
        manifest = out / ".foretoken-plots.json"
        if manifest.is_file():
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                previous["source"] = None
                manifest.write_text(json.dumps(previous, indent=2) + "\n", encoding="utf-8")
        return _render_results(source, *others, output_dir=out, columns=columns,
                               metrics=metrics, methods=methods, base=base)


def default_output_directory(source: Path, *others: Path, base: Path | None = None) -> Path:
    """Choose the shared default for the standalone CLI and automatic result exporter."""
    return Path("plots") if others or base is not None else Path(source) / "plots"


def _render_results(
    source: Path,
    *others: Path,
    output_dir: Path,
    columns: int,
    metrics: tuple[str, ...],
    methods: tuple[str, ...],
    base: Path | None,
) -> FigureExport:
    """Select a complete saved-result view, then replace only files owned by this export."""
    source = Path(source)
    out = output_dir
    if columns not in (1, 2):
        raise ValueError("columns must be 1 or 2")
    if base is None and not others:
        source = task_source(source)
    documents = read_records(source, ("config.json", "metrics.json", "sweep_points.json", "slo_results.json"))
    direct = base is None and not others and (
        "slo_results.json" in documents
        or "config.json" in documents and any(name in documents for name in ("metrics.json", "sweep_points.json"))
    )
    comparison = None if direct else read_saved_results((source, *map(Path, others)), methods=methods, base=base)
    command = read_records(source / "generated", ("context.json",))
    if base is None and comparison is not None and "context.json" in command and len(comparison.runs) == 1:
        measured = comparison.runs[0].get("result")
        collection_metrics = {row["metric"] for row in comparison.summary} | {
            chart.metric for chart in comparison.native
        }
        if comparison.runs[0].get("measured") and measured is not None and (
            not metrics or not set(metrics) <= collection_metrics
        ):
            return _render_results(Path(measured), output_dir=out, columns=columns, metrics=metrics,
                                   methods=comparison.method_filters.get(Path(measured), ()), base=None)
    if comparison is None and not any(name in documents for name in ("config.json", "slo_results.json")):
        raise FileNotFoundError(f"No saved benchmark config.json in {source}")
    config = documents.get("config.json", {})
    if comparison is not None:
        charts, identities, result = _render_collection(comparison, out, columns, metrics, base)
    elif "sweep_points.json" in documents:
        charts, identities, result = _render_sweep(source, config, out, columns, metrics, methods)
    elif "slo_results.json" in documents:
        search = documents["slo_results.json"]
        charts, identities = _slo_charts(search), []
        file = _table(out / "slo-probes", search["probes"])
        result = {file.name: file}
    elif "metrics.json" in documents:
        charts, identities, result = _render_run(source, config, out, methods)
    else:
        raise FileNotFoundError(f"No saved metrics or sweep results in {source}")
    if comparison is None and "sweep_points.json" not in documents:
        if set(methods) - set(identities):
            raise ValueError("Unknown plot methods: " + ", ".join(sorted(set(methods) - set(identities))))
        missing = set(metrics) - {chart.metric for chart in charts}
        if missing:
            raise ValueError("No measured values for plot metrics: " + ", ".join(sorted(missing)))
        if metrics:
            charts = [chart for chart in charts if chart.metric in metrics]
    result.update(_charts(
        charts, out,
        identities or list(dict.fromkeys(str(item.records[0].get("method") or item.name)
                                         for chart in charts for item in chart.series)),
        columns,
    ))
    # Redraws replace only files recorded by this exporter; unrelated files are retained.
    manifest = out / ".foretoken-plots.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    previous = previous["files"] if isinstance(previous, dict) else previous
    current = sorted(file.name for file in result.values())
    for filename in set(previous) - set(current):
        if Path(filename).name == filename:
            (out / filename).unlink(missing_ok=True)
    manifest.write_text(json.dumps({
        "files": current,
        "source": Path(os.path.relpath(source, out)).as_posix() if comparison is None else None,
    }, indent=2) + "\n", encoding="utf-8")
    return FigureExport(result, source.resolve() if comparison is None else None)


def _render_collection(
    comparison: SavedResults, out: Path, columns: int, metrics: tuple[str, ...], base: Path | None,
) -> tuple[list[Chart], list[str], dict[str, Path]]:
    """Export a comparison collection's planned runs, repeat statistics, and native score views."""
    base_rows = compare_to_base(comparison, base, metrics=metrics) if base is not None else None
    points, summary = comparison.points, comparison.summary
    scalar_metrics = {row["metric"] for row in summary}
    native_metrics = {chart.metric for chart in comparison.native}
    missing = set(metrics) - scalar_metrics - native_metrics
    if missing:
        raise ValueError("No measured values for plot metrics: " + ", ".join(sorted(missing)))
    identities = list(dict.fromkeys(_method(point) for point in points))
    charts = list(sweep_charts(points, summary, metrics=tuple(item for item in metrics if item in scalar_metrics), vary_axes=False)) if (
        points and (not metrics or set(metrics) & scalar_metrics)
    ) else []
    charts += [chart for chart in comparison.native if not metrics or chart.metric in metrics]
    result = _pareto(points, summary, out, (), columns) if points else {}
    run_rows = [{key: value for key, value in row.items() if key != "environment_settings"}
                for row in comparison.runs]
    for name, rows in (("comparison-runs", run_rows), ("comparison-summary", summary), ("comparison-base", base_rows)):
        if rows:
            file = _table(out / name, rows)
            result[file.name] = file
    return charts, identities, result


def _render_sweep(
    source: Path, config: dict[str, Any], out: Path, columns: int,
    metrics: tuple[str, ...], methods: tuple[str, ...],
) -> tuple[list[Chart], list[str], dict[str, Path]]:
    """Export an existing sweep's measured curves and selected summary, preserving repeat statistics."""
    points = read_record(source, "sweep_points.json")
    summary = read_record(source, "sweep_summary.json")
    identities = list(dict.fromkeys(_method(point) for point in points))
    charts = list(sweep_charts(points, summary, metrics=metrics, methods=methods))
    result = _pareto(points, summary, out, methods, columns) if config.get("mode") == "parameter_sweep" else {}
    if summary:
        selected = [row for row in summary if (not methods or _method(row) in methods)
                    and (not metrics or row["metric"] in metrics)]
        file = _table(out / "sweep-summary", selected)
        result[file.name] = file
    return charts, identities, result


def _render_run(
    source: Path, config: dict[str, Any], out: Path, methods: tuple[str, ...],
) -> tuple[list[Chart], list[str], dict[str, Path]]:
    """Export a single run's domain charts and native tables without measuring or rescoring it."""
    measured = read_record(source, "metrics.json")
    documents = read_records(source, ("warmup_metrics.json", "context.json"))
    warmup = documents.get("warmup_metrics.json")
    comparison = comparison_charts(measured)
    identities: list[str] = []
    if comparison is not None:
        charts, identities = comparison
        if methods:
            charts = [replace(chart, series=tuple(
                item for item in chart.series if str(item.records[0].get("method") or item.name) in methods
            )) for chart in charts]
            charts = [chart for chart in charts if chart.series]
    elif "scores" in measured:
        charts = _quality_charts(measured)
    elif config.get("mode") == "video_generation" or "requests" in config:
        charts = _video_phase_charts(source, measured)
        if warmup is not None:
            charts += phase_summary_charts(measured, warmup)
    else:
        charts = _http_charts(source, measured)
        if warmup is not None:
            charts += _http_charts(source, warmup, warmup=True)
            charts += phase_summary_charts(measured, warmup)
        charts += _prometheus_charts(source)
        charts += gpu_allocation_charts(source)
    result = {}
    if "evaluation_comparison" in measured or "context.json" in documents:
        runs = read_saved_results((source,), methods=methods).runs
        file = _table(out / "comparison-runs", [
            {key: value for key, value in row.items() if key != "environment_settings"} for row in runs
        ])
        result[file.name] = file
    if "scores" in measured:
        file = _table(out / "quality-scores", measured["scores"])
        result[file.name] = file
    if "distribution_comparison" in measured:
        comparison = measured["distribution_comparison"]
        for name, rows in (("distribution-candidates", comparison["candidates"]), ("distribution-positions", comparison["positions"])):
            file = _table(out / name, rows)
            result[file.name] = file
    return charts, identities, result
