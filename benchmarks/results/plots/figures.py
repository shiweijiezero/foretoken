# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render saved benchmark measurements as publication figures and inspectable tables."""

from __future__ import annotations

import csv
import json
import math
import re
import textwrap
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any


from benchmarks.results.plots.data import Chart, Series, _method, _numeric, sweep_charts
from benchmarks.results.plots.measurements import (
    _http_charts,
    gpu_allocation_charts,
    _prometheus_charts,
    _video_phase_charts,
    phase_summary_charts,
    _quality_charts,
    _distribution_charts,
    _greedy_charts,
    _evaluation_comparison_charts,
    _slo_charts,
)

_PALETTE = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
)
_MARKERS = ("o", "s", "^", "D", "v", "P", "X", "h")
_STYLES = ("-", "--", "-.", ":")
_FORMATS = ("pdf", "svg", "png")


def _table(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Export complete scalar and structured table records as CSV."""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    csv_path = path.with_name(path.name + ".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list, tuple))
                else value
                for key, value in row.items()
            }
            for row in rows
        )
    return csv_path


def _style(axis: Any, title: str, xlabel: str, ylabel: str) -> None:
    axis.set_title(
        textwrap.fill(title, width=46), loc="left", fontsize=9, color="#0b0b0b", pad=9
    )
    axis.set_xlabel(xlabel, fontsize=8)
    axis.set_ylabel(ylabel, fontsize=8)
    axis.grid(axis="y", color="#e1e0d9", linewidth=0.55)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#c3c2b7")
    axis.tick_params(labelsize=8, colors="#52514e", length=2)


def _save(fig: Any, path: Path) -> dict[str, Path]:
    """Write three print-ready formats from the same figure and release its canvas."""
    from matplotlib import rc_context

    paths = {}
    with rc_context({"pdf.fonttype": 42, "svg.fonttype": "none"}):
        for extension in _FORMATS:
            file = path.with_name(path.name + "." + extension)
            fig.savefig(file, dpi=220, facecolor="white")
            paths[extension] = file
    fig.clear()
    return paths


def _charts(
    charts: list[Chart] | tuple[Chart, ...],
    out: Path,
    identities: list[str],
    columns: int,
) -> dict[str, Path]:
    """Render each series with stable method colors, splitting dense legends into small figures."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    exported: dict[str, Path] = {}
    method_index = {name: i for i, name in enumerate(dict.fromkeys(identities))}
    used: dict[str, int] = defaultdict(int)
    for chart in charts:
        # Scatter panels use validated groups of three hues. Method colors
        # come from the complete unfiltered order in every figure.
        scatter = chart.kind in {"scatter", "pareto"}
        panels: dict[int, list[Series]] = defaultdict(list)
        for series in chart.series:
            identity = str(series.records[0].get("method") or series.name)
            index = method_index.setdefault(identity, len(method_index))
            panel = (index // 8) * 3 + (index % 8) // 3 if scatter else index // 8
            panels[panel].append(series)
        split_panels = [
            batch
            for panel in panels.values()
            for offset in range(0, len(panel), len(_PALETTE))
            if (batch := panel[offset : offset + len(_PALETTE)])
        ]
        for panel_number, series_list in enumerate(split_panels, 1):
            base = chart.name + (
                f"-panel-{panel_number}" if len(split_panels) > 1 else ""
            )
            used[base] += 1
            if used[base] > 1:
                base += f"-{used[base]}"
            path = out / base
            fig = Figure(
                figsize=(3.4 if columns == 1 else 7, 2.65 if columns == 1 else 3.1),
                layout="constrained",
            )
            FigureCanvasAgg(fig)
            axis = fig.add_subplot(111)
            for series in series_list:
                identity = str(series.records[0].get("method") or series.name)
                index = method_index[identity]
                slot = index % len(_PALETTE)
                xs, ys = list(series.x), list(series.y)
                style = dict(
                    color=_PALETTE[slot],
                    marker=_MARKERS[slot],
                    linestyle=_STYLES[slot % len(_STYLES)],
                    linewidth=1.5,
                    markersize=6,
                    markevery=1 if scatter else max(1, len(xs) // 12),
                    label=series.name,
                )
                if chart.kind == "step":
                    axis.step(
                        xs,
                        ys,
                        where="post",
                        color=_PALETTE[slot],
                        linewidth=1.5,
                        label=series.name,
                    )
                elif scatter:
                    axis.plot(xs, ys, **{**style, "linestyle": "none"})
                else:
                    axis.plot(xs, ys, **style)
                for x, y, error in zip(xs, ys, series.stddev):
                    if error is not None:
                        axis.errorbar(
                            x,
                            y,
                            yerr=error,
                            color=_PALETTE[slot],
                            linewidth=0.8,
                            capsize=2,
                            linestyle="none",
                        )
            if chart.kind == "pareto":
                frontier = []
                best = math.inf
                for x, y in sorted(
                    (x, y) for series in series_list for x, y in zip(series.x, series.y)
                ):
                    if y < best:
                        frontier.append((x, y))
                        best = y
                if len(frontier) > 1:
                    axis.plot(
                        [x for x, _ in frontier],
                        [y for _, y in frontier],
                        color="#52514e",
                        linestyle="--",
                        linewidth=1,
                        label="Min-cost / min-KL frontier",
                    )
            if chart.metric.startswith("gpu_allocation_"):
                axis.set_xlim(0, float(series_list[0].records[0]["duration_s"]))
                axis.set_ylim(bottom=0)
            if chart.yscale == "symlog":
                axis.set_yscale("symlog", linthresh=1e-6)
            figure_title = chart.title
            if len(series_list) == 1 and series_list[0].name not in {
                "Observed",
                "Native score",
                "Successful requests",
                "Probe average",
            }:
                figure_title += f" · {series_list[0].name}"
            _style(axis, figure_title, chart.xlabel, chart.ylabel)
            if chart.metric in {
                "slo_slo_attainment", "exact_match_rate", "Time/SLO attainment (%)",
                "speculative_decoding_acceptance_ratio", "speculative_decoding_draft_time_share_ratio",
                "speculative_decoding_target_forward_time_share_ratio",
            }:
                axis.set_ylim(-3, 103)
                axis.set_yticks(range(0, 101, 20))
            if chart.tick_labels:
                ticks = sorted({x for series in series_list for x in series.x})
                axis.set_xticks(
                    ticks,
                    [chart.tick_labels[int(x)] for x in ticks],
                    rotation=25,
                    ha="right",
                )
            if len(series_list) > 1 or chart.kind == "pareto":
                axis.legend(fontsize=8, frameon=False, loc="best")
            for extension, file in _save(fig, path).items():
                exported[f"{base}.{extension}"] = file
            rows = []
            for series in series_list:
                for x, y, error, record in zip(
                    series.x, series.y, series.stddev, series.records
                ):
                    rows.append(
                        {
                            "series": series.name,
                            "x_axis": chart.xlabel,
                            "y_axis": chart.ylabel,
                            "x": x,
                            "x_label": chart.tick_labels[int(x)]
                            if chart.tick_labels
                            else x,
                            "y": y,
                            "error": error,
                            "error_type": record.get(
                                "error_type", "run stddev" if error is not None else ""
                            ),
                            **record,
                        }
                    )
            file = _table(path, rows)
            exported[file.name] = file
    return exported


def _pareto(
    points: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    out: Path,
    methods: tuple[str, ...],
    columns: int,
) -> dict[str, Path]:
    """Compare per-user and per-GPU output throughput on independently grouped workloads."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    point_by_key = {
        (
            _method(p),
            str(p["combination"]),
        ): p
        for p in points
    }
    xy: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    fields = (
        "generation_tokens_per_second_per_user",
        "generation_tokens_per_second_per_gpu",
    )
    for row in summary:
        if row["metric"] not in fields or _numeric(row.get("mean")) is None:
            continue
        method = _method(row)
        if methods and method not in methods:
            continue
        xy[(method, str(row["combination"]))][row["metric"]] = row
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (method, combination), values in xy.items():
        if not all(key in values for key in fields):
            continue
        row = values[fields[0]]
        point = point_by_key.get((method, combination), {})
        bench = row.get("bench") or point.get("bench") or {}
        fixed = json.dumps(
            {
                key: value
                for key, value in bench.items()
                if key not in {"service", "max_concurrency"} and not key.startswith("_")
            },
            sort_keys=True,
        )
        group = str(
            row.get("parameter_group") or point.get("parameter_group") or "default"
        )
        groups[(group, fixed)].append(
            {
                "method": method,
                "combination": combination,
                "parameter_group": group,
                "per_user_tokens_per_second": values[fields[0]]["mean"],
                "per_gpu_tokens_per_second": values[fields[1]]["mean"],
                "per_user_stddev": values[fields[0]].get("stddev"),
                "per_gpu_stddev": values[fields[1]].get("stddev"),
                "per_user_samples": values[fields[0]].get("samples"),
                "per_gpu_samples": values[fields[1]].get("samples"),
                "failed_runs": row.get("failed_runs"),
                "requested_runs": row.get("requested_runs"),
                "error_type": "run stddev",
                "max_concurrency": bench.get(
                    "max_concurrency", point.get("max_concurrency")
                ),
                "bench": fixed,
            }
        )
    identities = list(dict.fromkeys(_method(point) for point in points))
    exported = {}
    for index, ((group, fixed), rows) in enumerate(groups.items(), 1):
        # Scatter markers have all-pairs relationships: limit each small figure
        # to three distinct method hues, retaining the unfiltered color slot.
        panels: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            method_index = identities.index(row["method"])
            panels[(method_index // 8) * 3 + (method_index % 8) // 3].append(row)
        for panel, plotted in panels.items():
            base = (
                f"pareto-{index}-{re.sub('[^a-z0-9]+', '-', group.lower()).strip('-')}"
            )
            if len(panels) > 1:
                base += f"-panel-{panel + 1}"
            path = out / base
            figure = Figure(
                figsize=(3.4 if columns == 1 else 7, 2.8 if columns == 1 else 3.5),
                layout="constrained",
            )
            FigureCanvasAgg(figure)
            axis = figure.add_subplot(111)
            for method in dict.fromkeys(row["method"] for row in plotted):
                subset = [row for row in plotted if row["method"] == method]
                slot = identities.index(method) % len(_PALETTE)
                color = _PALETTE[slot]
                # Color identifies the method; marker area encodes configured concurrency.
                axis.scatter(
                    [row["per_user_tokens_per_second"] for row in subset],
                    [row["per_gpu_tokens_per_second"] for row in subset],
                    s=[
                        20 + 5 * max(0, float(row["max_concurrency"] or 0))
                        for row in subset
                    ],
                    color=color,
                    marker=_MARKERS[slot],
                    edgecolor="white",
                    linewidth=0.7,
                    label=method,
                )
                for row in subset:
                    xerr = _numeric(row["per_user_stddev"])
                    yerr = _numeric(row["per_gpu_stddev"])
                    if xerr is not None or yerr is not None:
                        axis.errorbar(
                            row["per_user_tokens_per_second"],
                            row["per_gpu_tokens_per_second"],
                            xerr=xerr,
                            yerr=yerr,
                            color=color,
                            linewidth=0.8,
                            capsize=2,
                            linestyle="none",
                        )
                frontier = []
                best = -math.inf
                for row in sorted(
                    subset,
                    key=lambda item: (
                        -item["per_user_tokens_per_second"],
                        -item["per_gpu_tokens_per_second"],
                    ),
                ):
                    if row["per_gpu_tokens_per_second"] > best:
                        frontier.append(row)
                        best = row["per_gpu_tokens_per_second"]
                if len(frontier) > 1:
                    axis.plot(
                        [row["per_user_tokens_per_second"] for row in frontier],
                        [row["per_gpu_tokens_per_second"] for row in frontier],
                        color=color,
                        linewidth=1,
                        linestyle="--",
                    )
            title = f"Pareto · {group}"
            if len({row["method"] for row in plotted}) == 1:
                title += f" · {plotted[0]['method']}"
            _style(
                axis,
                title,
                "Output throughput per user (tokens/s)",
                "Output throughput per GPU (tokens/s)",
            )
            if len({row["method"] for row in plotted}) > 1:
                axis.legend(fontsize=8, frameon=False)
            for extension, file in _save(figure, path).items():
                exported[f"{base}.{extension}"] = file
            file = _table(path, plotted)
            exported[file.name] = file
    return exported


def render_results(
    source: Path,
    *,
    output_dir: Path | None = None,
    columns: int = 1,
    metrics: tuple[str, ...] = (),
    methods: tuple[str, ...] = (),
) -> dict[str, Path]:
    """Re-render a saved perf, sweep, video or evaluation directory into figures and tables.

    The caller owns the source and destination directory. This function only reads
    persisted measurement and summary artifacts; it neither runs benchmarks nor
    changes the existing JSON or native evaluator outputs.
    """
    source = Path(source)
    out = Path(output_dir) if output_dir is not None else source / "plots"
    if columns not in (1, 2):
        raise ValueError("columns must be 1 or 2")
    config_path = source / "config.json"
    if not config_path.is_file() and not (source / "slo_results.json").is_file():
        raise FileNotFoundError(f"No saved benchmark config.json in {source}")
    config = (
        json.loads((config_path).read_text(encoding="utf-8"))
        if config_path.is_file()
        else {}
    )
    charts: list[Chart] = []
    identities: list[str] = []
    points_path = source / "sweep_points.json"
    if points_path.is_file():
        points = json.loads((points_path).read_text(encoding="utf-8"))
        summary = json.loads(
            (source / "sweep_summary.json").read_text(encoding="utf-8")
        )
        identities = list(dict.fromkeys(_method(point) for point in points))
        charts = list(sweep_charts(points, summary, metrics=metrics, methods=methods))
        out.mkdir(parents=True, exist_ok=True)
        result = (
            _pareto(points, summary, out, methods, columns)
            if config.get("mode") == "parameter_sweep"
            else {}
        )
        if summary:
            selected_summary = [
                row
                for row in summary
                if (not methods or _method(row) in methods)
                and (not metrics or row["metric"] in metrics)
            ]
            file = _table(out / "sweep-summary", selected_summary)
            result[file.name] = file
    elif (source / "slo_results.json").is_file():
        search = json.loads((source / "slo_results.json").read_text(encoding="utf-8"))
        charts = _slo_charts(search)
        out.mkdir(parents=True, exist_ok=True)
        result = {}
        file = _table(out / "slo-probes", search["probes"])
        result[file.name] = file
    elif (source / "metrics.json").is_file():
        run_metrics = json.loads((source / "metrics.json").read_text(encoding="utf-8"))
        warmup_metrics_path = source / "warmup_metrics.json"
        warmup_metrics = json.loads(warmup_metrics_path.read_text(encoding="utf-8")) if warmup_metrics_path.is_file() else None
        if "evaluation_comparison" in run_metrics:
            comparison = run_metrics["evaluation_comparison"]
            identities = [method["label"] for method in comparison["methods"]]
            charts = _evaluation_comparison_charts(comparison)
            if methods:
                charts = [replace(chart, series=tuple(item for item in chart.series
                                                      if item.name in methods)) for chart in charts]
                charts = [chart for chart in charts if chart.series]
        elif "greedy_comparison" in run_metrics:
            comparison = run_metrics["greedy_comparison"]
            identities = list(dict.fromkeys(str(point["method"]) for point in comparison["candidates"]))
            charts = _greedy_charts(comparison)
            if methods:
                charts = [replace(chart, series=tuple(
                    item for item in chart.series
                    if str(item.records[0].get("method") or item.name) in methods
                )) for chart in charts]
                charts = [chart for chart in charts if chart.series]
        elif "distribution_comparison" in run_metrics:
            comparison = run_metrics["distribution_comparison"]
            identities = list(
                dict.fromkeys(
                    str(point["method"]) for point in comparison["candidates"]
                )
            )
            charts = _distribution_charts(comparison)
            if methods:
                charts = [
                    replace(
                        chart,
                        series=tuple(
                            series
                            for series in chart.series
                            if str(series.records[0].get("method") or series.name)
                            in methods
                        ),
                    )
                    for chart in charts
                ]
                charts = [chart for chart in charts if chart.series]
        elif "scores" in run_metrics:
            charts = _quality_charts(run_metrics)
        elif (
            config.get("mode") == "video_generation"
            or (source / "raw_results.json").is_file()
        ):
            charts = _video_phase_charts(source, run_metrics)
            if warmup_metrics is not None:
                charts += phase_summary_charts(run_metrics, warmup_metrics)
        else:
            charts = _http_charts(source, run_metrics)
            if warmup_metrics is not None:
                charts += _http_charts(source, warmup_metrics, warmup=True)
                charts += phase_summary_charts(run_metrics, warmup_metrics)
            charts += _prometheus_charts(source)
            charts += gpu_allocation_charts(source)
        out.mkdir(parents=True, exist_ok=True)
        result = {}
        if "scores" in run_metrics:
            file = _table(out / "quality-scores", run_metrics["scores"])
            result[file.name] = file
        if "distribution_comparison" in run_metrics:
            comparison = run_metrics["distribution_comparison"]
            for name, rows in (
                ("distribution-candidates", comparison["candidates"]),
                ("distribution-positions", comparison["positions"]),
            ):
                file = _table(out / name, rows)
                result[file.name] = file
    else:
        raise FileNotFoundError(f"No saved metrics or sweep results in {source}")
    if not points_path.is_file():
        if set(methods) - set(identities):
            raise ValueError(
                "Unknown plot methods: "
                + ", ".join(sorted(set(methods) - set(identities)))
            )
        missing = set(metrics) - {chart.metric for chart in charts}
        if missing:
            raise ValueError(
                "No measured values for plot metrics: " + ", ".join(sorted(missing))
            )
        if metrics:
            charts = [chart for chart in charts if chart.metric in metrics]
    result.update(
        _charts(
            charts,
            out,
            identities
            or list(dict.fromkeys(s.name for chart in charts for s in chart.series)),
            columns,
        )
    )
    # Redraws replace only files recorded by this exporter; unrelated files are retained.
    manifest = out / ".foretoken-plots.json"
    previous = (
        json.loads((manifest).read_text(encoding="utf-8")) if manifest.is_file() else []
    )
    current = sorted(file.name for file in result.values())
    for filename in set(previous) - set(current):
        if Path(filename).name == filename:
            (out / filename).unlink(missing_ok=True)
    manifest.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return result
