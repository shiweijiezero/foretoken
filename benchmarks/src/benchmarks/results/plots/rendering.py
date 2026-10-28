# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render chart coordinates and their complete tables with one Matplotlib backend."""

from __future__ import annotations

import csv
import json
import math
import textwrap
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from benchmarks.results.plots.data import Chart, Series

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


class FigureWriter:
    """Track artifacts written under the figure exporter's directory lock."""

    def __init__(self, directory: Path) -> None:
        """Use the prepared output directory and retain attempted files for manifest recovery."""
        self.directory = directory
        self.files: dict[str, Path] = {}

    def table(self, name: str, rows: list[dict[str, Any]]) -> Path:
        """Register and write a CSV containing every scalar and structured record field."""
        path = self.directory / f"{name}.csv"
        self.files[path.name] = path
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list, tuple)) else value
                    for key, value in row.items()
                }
                for row in rows
            )
        return path

    def figure(self, figure: Any, name: str, *, description: str | None = None) -> None:
        """Register each figure format before writing it and release its canvas after export."""
        from matplotlib import rc_context

        try:
            with rc_context({"pdf.fonttype": 42, "svg.fonttype": "none"}):
                for extension in _FORMATS:
                    path = self.directory / f"{name}.{extension}"
                    self.files[path.name] = path
                    metadata = None if description is None else {
                        "Subject" if extension == "pdf" else "Description": description,
                    }
                    figure.savefig(path, dpi=220, facecolor="white", metadata=metadata)
        finally:
            figure.clear()


def pareto_frontier(
    points: Iterable[tuple[float, float]], objectives: str,
) -> list[tuple[float, float]]:
    """Return nondominated coordinates for the renderers' x/y min/max objectives."""
    x_goal, y_goal = (1 if direction == "max" else -1 for direction in objectives.split("-"))
    best = -math.inf
    frontier = []
    for x, y in sorted(points, key=lambda point: (-x_goal * point[0], -y_goal * point[1])):
        value = y_goal * y
        if value > best:
            frontier.append((x, y))
            best = value
    return frontier


def _charts(
    charts: list[Chart] | tuple[Chart, ...],
    writer: FigureWriter,
    identities: list[str],
    columns: int,
) -> None:
    """Render each series with stable method colors, splitting dense legends into small figures."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

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
                style = {
                    "color": _PALETTE[slot],
                    "marker": _MARKERS[slot],
                    "linestyle": _STYLES[slot % len(_STYLES)],
                    "linewidth": 1.5,
                    "markersize": 6,
                    "markevery": 1 if scatter else max(1, len(xs) // 12),
                    "label": series.name,
                }
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
                errors = [(x, y, error) for x, y, error in zip(xs, ys, series.stddev) if error is not None]
                if errors:
                    error_x, error_y, yerr = zip(*errors)
                    axis.errorbar(
                        error_x, error_y, yerr=yerr, fmt="none",
                        color=_PALETTE[slot], linewidth=0.8, capsize=2,
                    )
            if chart.kind == "pareto":
                frontier = pareto_frontier(
                    ((x, y) for series in series_list for x, y in zip(series.x, series.y)),
                    "min-min",
                )
                if len(frontier) > 1:
                    axis.plot(
                        [x for x, _ in frontier],
                        [y for _, y in frontier],
                        color="#52514e",
                        linestyle="--",
                        linewidth=1,
                        label="Min-cost / min-KL frontier",
                    )
            if chart.kind == "step" and chart.metric.startswith("gpu_allocation_"):
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
                _legend(fig, axis, columns)
            writer.figure(fig, base)
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
            writer.table(base, rows)


def _style(axis: Any, title: str, xlabel: str, ylabel: str) -> None:
    axis.set_title(
        title, wrap=True, loc="left", fontsize=9, color="#0b0b0b", pad=9
    )
    axis.set_xlabel(xlabel, fontsize=8)
    axis.set_ylabel(ylabel, fontsize=8)
    axis.grid(axis="y", color="#e1e0d9", linewidth=0.55)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#c3c2b7")
    axis.tick_params(labelsize=8, colors="#52514e", length=2)


def _legend(fig: Any, axis: Any, columns: int) -> None:
    """Keep complete series identities readable without covering measured curves."""
    handles, labels = axis.get_legend_handles_labels()
    labels = [textwrap.fill(label, width=38 if columns == 1 else 80) for label in labels]
    if any("\n" in label for label in labels):
        lines = sum(label.count("\n") + 1 for label in labels)
        fig.set_size_inches(fig.get_figwidth(), fig.get_figheight() + lines * 0.12)
        fig.legend(handles, labels, fontsize=8, frameon=False, loc="outside lower center")
    else:
        axis.legend(handles, labels, fontsize=8, frameon=False, loc="best")
