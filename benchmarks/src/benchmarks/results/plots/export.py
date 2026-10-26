# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select saved-result views and own figure publication, including incomplete exports."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from benchmarks.config.plot import DEFAULT_INTERACTIVITY, MetricSelection, TradeoffOptions
from benchmarks.results.plots.charts.http import _http_charts
from benchmarks.results.plots.charts.phases import phase_summary_charts
from benchmarks.results.plots.charts.resources import _prometheus_charts, gpu_allocation_charts
from benchmarks.results.plots.charts.video import _video_phase_charts
from benchmarks.results.plots.comparison import compare_to_base
from benchmarks.results.plots.data import Chart, _method, sweep_charts
from benchmarks.results.plots.economics import PRICED_TOKEN_METRICS, Pricing, load_pricing, render_economics
from benchmarks.results.plots.metrics import derived_plot_metrics
from benchmarks.results.plots.rendering import FigureWriter, _charts
from benchmarks.results.plots.saved_results import SavedResults, read_saved_results
from benchmarks.results.plots.tradeoffs import render_tradeoffs
from benchmarks.results.recording.database import read_records
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
    tradeoff: TradeoffOptions | None = None,
    pricing_path: Path | None = None,
    min_interactivity: float | None = None,
    interactivity: MetricSelection = DEFAULT_INTERACTIVITY,
    gpu_count: int | None = None,
    progress: bool = False,
) -> FigureExport:
    """Export saved measurements under the same directory lock used by W&B publication.

    Every renderer registers files before writing. Failed exports retain their
    diagnostics without a publication owner; the next successful redraw removes
    superseded files while preserving unrelated contents of the output directory.
    """
    if columns not in (1, 2):
        raise ValueError("columns must be 1 or 2")
    sources = (Path(source), *map(Path, others))
    if progress:
        if (
            columns != 1 or metrics or methods or base is not None or tradeoff is not None
            or pricing_path is not None or min_interactivity is not None or gpu_count is not None
            or interactivity != DEFAULT_INTERACTIVITY
        ):
            raise ValueError("Progress export accepts sources and output_dir; plot settings come from the YAML files")
        from benchmarks.results.plots.progress import load_progress, render_progress

        sources = tuple(dict.fromkeys(path.resolve() for path in sources))
        if len({path.stem for path in sources}) != len(sources):
            raise ValueError("Progress data files need distinct names when exported together")
        progress_data = [(path, load_progress(path)) for path in sources]
    else:
        for selected in (*sources, *((base,) if base is not None else ())):
            if not selected.is_dir():
                raise NotADirectoryError(f"Result directory does not exist: {selected}")
    out = (Path(output_dir) if output_dir is not None else default_output_directory(
        *sources, base=base, progress=progress,
    )).resolve()
    pricing = load_pricing(pricing_path) if pricing_path is not None else None
    out.mkdir(parents=True, exist_ok=True)
    with publication_lock(out, scope="plots"):
        manifest = out / ".foretoken-plots.json"
        previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
        previous = set(previous["files"] if isinstance(previous, dict) else previous)
        _record_export(manifest, previous, None)
        writer = FigureWriter(out)
        try:
            if progress:
                for path, data in progress_data:
                    render_progress(data, path, writer)
                owner = None
            else:
                owner = _render_results(
                    sources, writer, columns=columns, metrics=metrics, methods=methods, base=base,
                    tradeoff=tradeoff, pricing=pricing, min_interactivity=min_interactivity,
                    interactivity=interactivity,
                    gpu_count=gpu_count,
                )
            for filename in previous - writer.files.keys():
                if Path(filename).name == filename:
                    (out / filename).unlink(missing_ok=True)
            _record_export(manifest, writer.files, owner)
        except BaseException:
            try:
                _record_export(manifest, previous | writer.files.keys(), None)
            except OSError:
                logger.exception("Could not record the incomplete figure export in {}", manifest)
            raise
    return FigureExport(writer.files, owner)


def default_output_directory(
    source: Path, *others: Path, base: Path | None = None, progress: bool = False,
) -> Path:
    """Choose the shared default for the standalone CLI and automatic result exporter."""
    if progress:
        parents = {Path(path).resolve().parent for path in (source, *others)}
        return parents.pop() if len(parents) == 1 else Path("plots")
    return Path("plots") if others or base is not None else Path(source) / "plots"


def _record_export(manifest: Path, files: Iterable[str], owner: Path | None) -> None:
    """Publish the files owned by this export and its optional measured-run identity."""
    manifest.write_text(json.dumps({
        "files": sorted(files),
        "source": Path(os.path.relpath(owner, manifest.parent)).as_posix() if owner is not None else None,
    }, indent=2) + "\n", encoding="utf-8")


def _render_results(
    sources: tuple[Path, ...], writer: FigureWriter, *, columns: int,
    metrics: tuple[str, ...], methods: tuple[str, ...], base: Path | None,
    tradeoff: TradeoffOptions | None, pricing: Pricing | None,
    min_interactivity: float | None, interactivity: MetricSelection, gpu_count: int | None,
) -> Path | None:
    """Resolve one set of identities and statistics before selecting any chart family."""
    selected_metrics = set(metrics)
    axes = tradeoff or TradeoffOptions()
    if tradeoff is not None or not metrics:
        selected_metrics.update((axes.x.key, axes.y.key))
    if pricing is not None:
        selected_metrics.update(PRICED_TOKEN_METRICS)
        if min_interactivity is not None:
            selected_metrics.add(interactivity.key)
    derived = derived_plot_metrics(selected_metrics)
    results = read_saved_results(
        sources, methods=methods, base=base, derive_metrics=bool(derived) or gpu_count is not None,
        derived_metrics=derived, gpu_count=gpu_count, pricing=pricing,
    )

    # A recorded aggregate owns its own figures. Containers with one measured
    # leaf borrow that leaf's identity; comparisons have no single publisher.
    owner = None
    source_records = read_records(task_source(sources[0]), ("config.json", "metrics.json", "sweep_points.json", "slo_results.json"))
    if len(sources) == 1 and base is None:
        if any(name in source_records for name in ("metrics.json", "sweep_points.json", "slo_results.json")):
            owner = task_source(sources[0]).resolve()
        elif len(results.runs) == 1 and results.runs[0].get("result") is not None:
            owner = Path(results.runs[0]["result"]).resolve()
    single_run = (
        len(results.runs) == 1 and len(sources) == 1 and base is None
        and "sweep_points.json" not in source_records
        and source_records.get("config.json", {}).get("mode") not in {"parameter_sweep", "video_parameter_sweep"}
    )
    native = list(results.native)
    if single_run and not native and results.runs[0].get("result") is not None:
        native = _single_run_charts(Path(results.runs[0]["result"]))
    _render_views(results, native, writer, columns, metrics, base, tradeoff, pricing,
                  min_interactivity, interactivity, single_run=single_run)
    if owner is not None:
        selected_methods = {method for run in results.runs for method in run.get("methods", ())}
        _native_tables(owner, writer, selected_methods)
    return owner


def _render_views(
    results: SavedResults, native: list[Chart], writer: FigureWriter,
    columns: int, metrics: tuple[str, ...], base: Path | None,
    tradeoff: TradeoffOptions | None, pricing: Pricing | None, min_interactivity: float | None,
    interactivity: MetricSelection,
    *, single_run: bool,
) -> None:
    """Apply metric selection consistently to native charts and run-level scalar summaries."""
    points, summary = results.points, results.summary
    scalar_metrics = {row["metric"] for row in summary}
    missing = set(metrics) - scalar_metrics - {chart.metric for chart in native}
    if missing:
        raise ValueError("No measured values for plot metrics: " + ", ".join(sorted(missing)))
    base_rows = compare_to_base(results, base, metrics=metrics) if base is not None else None
    defaults = tradeoff is None and pricing is None and not metrics
    selected_scalars = tuple(metric for metric in metrics if metric in scalar_metrics)
    charts = [chart for chart in native if defaults or chart.metric in metrics]
    if points and (selected_scalars or defaults and (not single_run or not native)):
        charts.extend(sweep_charts(points, summary, metrics=selected_scalars, vary_axes=True))
    if tradeoff is not None or defaults and not single_run:
        render_tradeoffs(points, summary, writer, (), columns, options=tradeoff,
                         base_methods=results.base_approaches)
    if pricing is not None:
        render_economics(points, summary, writer, pricing,
                         min_interactivity=min_interactivity, interactivity=interactivity,
                         columns=columns)
    identities = list(dict.fromkeys(_method(point) for point in points))
    _charts(charts, writer, identities, columns)
    run_rows = [{key: value for key, value in row.items() if key != "environment_settings"}
                for row in results.runs]
    for name, rows in (("comparison-runs", run_rows), ("comparison-summary", summary), ("comparison-base", base_rows)):
        if rows:
            writer.table(name, rows)


def _single_run_charts(source: Path) -> list[Chart]:
    """Read request and resource views for one run, independently of metadata enrichment."""
    documents = read_records(source, ("config.json", "metrics.json", "warmup_metrics.json"))
    measured = documents.get("metrics.json")
    if measured is None or "scores" in measured:
        return []
    config = documents.get("config.json", {})
    warmup = documents.get("warmup_metrics.json")
    if config.get("mode") == "video_generation" or "requests" in config:
        charts = _video_phase_charts(source, measured)
    else:
        charts = _http_charts(source, measured)
        if warmup is not None:
            charts += _http_charts(source, warmup, warmup=True)
        charts += _prometheus_charts(source)
        charts += gpu_allocation_charts(source)
    if warmup is not None:
        charts += phase_summary_charts(measured, warmup)
    return charts


def _native_tables(source: Path, writer: FigureWriter, methods: set[str]) -> None:
    """Retain the evaluator and SLO tables alongside their run's exported views."""
    documents = read_records(source, ("metrics.json", "slo_results.json"))
    measured = documents.get("metrics.json", {})
    if "slo_results.json" in documents:
        writer.table("slo-probes", documents["slo_results.json"]["probes"])
    if "scores" in measured:
        writer.table("quality-scores", [row for row in measured["scores"]
                                       if "method" not in row or row["method"] in methods])
    if "distribution_comparison" in measured:
        comparison = measured["distribution_comparison"]
        candidates = [row for row in comparison["candidates"] if row["method"] in methods]
        labels = {row["label"] for row in candidates}
        writer.table("distribution-candidates", candidates)
        writer.table("distribution-positions", [row for row in comparison["positions"] if row["candidate"] in labels])
