# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compare measured axes while preserving workload slices and paired repetitions."""

from __future__ import annotations

from typing import Any

from benchmarks.config.plot import TradeoffOptions
from benchmarks.results.plots.data import (
    _method,
    _metric_display,
    _numeric,
    _slug,
    paired_summaries,
    workload_slices,
)
from benchmarks.results.plots.rendering import (
    _MARKERS,
    _PALETTE,
    FigureWriter,
    _legend,
    _style,
    pareto_frontier,
)


def render_tradeoffs(
    points: list[dict[str, Any]], summary: list[dict[str, Any]], writer: FigureWriter,
    methods: tuple[str, ...], columns: int, *, options: TradeoffOptions | None = None,
    base_methods: tuple[str, ...] = (),
) -> None:
    """Export annotated curves and matched-base changes from paired run measurements.

    Only the selected sweep parameter varies along a curve. Both coordinates
    use the same repetitions; missing coordinates and failed-run counts remain
    in the accompanying CSV. Curves follow either the selected Pareto objectives
    or the sweep parameter, keeping measured points visible in both views.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    strict = options is not None
    options = options or TradeoffOptions()
    identities = list(dict.fromkeys(_method(point) for point in points))
    selected = [method for method in identities if not methods or method in methods]
    rows = _coordinates(points, summary, options, selected)
    if not any(row["status"] == "measured" for row in rows):
        if strict:
            raise ValueError(f"No paired measurements for {options.x.key} and {options.y.key}; "
                             "per-GPU axes require recorded GPU capacity or --gpu-count")
        return
    base = base_methods[0] if len(base_methods) == 1 else None
    for index, (group, details, group_rows) in enumerate(workload_slices(rows, options.sweep_axis), 1):
        _base_changes(group_rows, base, options.compare_axis)
        name = f"tradeoff-{index}-{_slug(group)}"
        writer.table(name, group_rows)
        plotted = [row for row in group_rows if row["status"] == "measured"]
        names = [method for method in selected if any(row["method"] == method for row in plotted)]
        others = [method for method in names if method != base]
        panels = ([([base] if base in names else []) + others[start:start + 2]
                   for start in range(0, len(others), 2)] if base in names
                  else [names[start:start + 3] for start in range(0, len(names), 3)])
        if names == [base]:
            panels = [[base]]
        for panel, panel_methods in enumerate(panels, 1):
            figure = Figure(figsize=(3.4 if columns == 1 else 7, 3.2 if columns == 1 else 4.4),
                            layout="constrained")
            FigureCanvasAgg(figure)
            axis = figure.add_subplot(111)
            annotations = []
            for method in panel_methods:
                subset = [row for row in plotted if row["method"] == method]
                subset.sort(key=lambda row: (_numeric(row["bench"].get(options.sweep_axis)) is None,
                                             _numeric(row["bench"].get(options.sweep_axis)) or 0,
                                             row["combination"]))
                slot = identities.index(method) % len(_PALETTE)
                color = _PALETTE[slot]
                connected = all(_numeric(row["bench"].get(options.sweep_axis)) is not None for row in subset)
                axis.plot([row["x"] for row in subset], [row["y"] for row in subset],
                          color=color, marker=_MARKERS[slot], markersize=5,
                          markeredgecolor="white", markeredgewidth=0.7,
                          linestyle="-" if connected and options.frontier == "none" else "none",
                          linewidth=1.3, label=method)
                if options.frontier != "none":
                    frontier = pareto_frontier(((row["x"], row["y"]) for row in subset), options.frontier)
                    axis.plot([x for x, _ in frontier], [y for _, y in frontier],
                              color=color, linestyle="--", linewidth=1.3)
                for coordinate in ("x", "y"):
                    errors = [row for row in subset if row[f"{coordinate}_stddev"] is not None]
                    if errors:
                        axis.errorbar([row["x"] for row in errors], [row["y"] for row in errors],
                                      **{f"{coordinate}err": [row[f"{coordinate}_stddev"] for row in errors]},
                                      fmt="none", color=color, linewidth=0.8, capsize=2)
                for row in subset:
                    label = _label(row, options)
                    if label:
                        annotations.append((row["x"], row["y"], label, color))
                    if row.get("change_percent") is not None:
                        start = (row["base_x"], row["base_y"])
                        axis.plot([start[0], row["x"]], [start[1], row["y"]],
                                  color="#777777", linestyle=":", linewidth=0.9)
                        annotations.append(((start[0] + row["x"]) / 2, (start[1] + row["y"]) / 2,
                                            f"{options.compare_axis}: {row['change_percent']:+.1f}%", "#333333"))
            title = f"Performance trade-off · {group}" + (f"\n{details}" if details else "")
            _style(axis, title, _metric_display(options.x.key)[1], _metric_display(options.y.key)[1])
            axis.margins(x=0.16, y=0.19)
            if len(panel_methods) > 1:
                _legend(figure, axis, columns)
            _annotations(figure, axis, annotations)
            writer.figure(figure, name + (f"-panel-{panel}" if len(panels) > 1 else ""))


def _coordinates(
    points: list[dict[str, Any]], summary: list[dict[str, Any]], options: TradeoffOptions,
    methods: list[str],
) -> list[dict[str, Any]]:
    """Pair axes within each repetition before computing their mean and error bars."""
    coordinates = paired_summaries(
        [point for point in points if _method(point) in methods],
        (options.x.key, options.y.key),
    )
    attempts = {(_method(row), str(row["combination"])): row for row in summary}
    identities = {(_method(point), str(point["combination"])): point for point in points
                  if _method(point) in methods}
    rows = []
    for (method, combination), point in identities.items():
        counts = attempts.get((method, combination), {})
        row = {"method": method, "combination": combination,
               "parameter_group": str(point.get("parameter_group", "default")),
               "bench": point.get("bench", {}),
               "status": "measured", "paired_samples": 0,
               "runs": counts.get("runs"), "requested_runs": counts.get("requested_runs"),
               "failed_runs": counts.get("failed_runs"), "error_type": "run stddev"}
        for coordinate, selection in (("x", options.x), ("y", options.y)):
            values = coordinates.get((method, combination), {}).get(selection.key)
            scale, label = _metric_display(selection.key)
            row.update({f"{coordinate}_metric": selection.metric, f"{coordinate}_stat": selection.stat,
                        f"{coordinate}_metric_key": selection.key, f"{coordinate}_axis": label,
                        coordinate: values["mean"] * scale if values else None,
                        f"{coordinate}_stddev": values["stddev"] * scale
                        if values and values["stddev"] is not None else None})
            if values:
                row["paired_samples"] = values["samples"]
            else:
                row["status"] = "missing_paired_metrics"
        rows.append(row)
    return rows


def _base_changes(rows: list[dict[str, Any]], base: str | None, coordinate: str) -> None:
    """Connect only equal-workload points and retain missing comparisons in the table."""
    if base is None:
        return
    references = {row["combination"]: row for row in rows if row["method"] == base}
    for row in rows:
        if row["method"] == base:
            continue
        reference = references.get(row["combination"])
        row.update(base=base, comparison_axis=coordinate, change_percent=None,
                   comparison_status="missing_base_workload")
        if reference is None:
            continue
        row.update(base_x=reference["x"], base_y=reference["y"])
        if row["status"] != "measured" or reference["status"] != "measured":
            row["comparison_status"] = "missing_paired_metrics"
        elif reference[coordinate] == 0:
            row["comparison_status"] = "zero_base"
        else:
            row.update(comparison_status="compared",
                       change_percent=(row[coordinate] / reference[coordinate] - 1) * 100)


def _label(row: dict[str, Any], options: TradeoffOptions) -> str:
    labels = []
    for field in options.label_by:
        value = row["bench"].get(field)
        if value is not None:
            labels.append(f"c{value}" if field == "max_concurrency" else f"{field}={value}")
    label = ", ".join(labels)
    if options.label_values:
        values = [f"{row[key]:,.0f}" if abs(row[key]) >= 1000 else f"{row[key]:,.1f}" for key in ("x", "y")]
        label += ("\n" if label else "") + f"({values[0]}, {values[1]})"
    return label


def _annotations(figure: Any, axis: Any, labels: list[tuple[float, float, str, str]]) -> None:
    """Place short labels inside the axes, preferring offsets with fewer overlaps."""
    from matplotlib.transforms import Bbox

    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    occupied = [Bbox.from_bounds(px - 4, py - 4, 8, 8)
                for x, y, _, _ in labels for px, py in [axis.transData.transform((x, y))]]
    legend = axis.get_legend()
    if legend is not None:
        occupied.append(legend.get_window_extent(renderer))
    curves = [line.get_path().transformed(line.get_transform()) for line in axis.lines
              if line.get_linestyle() not in {"None", "none", "", " "}]
    bounds = axis.get_window_extent(renderer)
    for x, y, label, color in labels:
        annotation = axis.annotate(label, (x, y), xytext=(7, 7), textcoords="offset points",
                                   fontsize=7, color=color, ha="left", va="bottom")
        annotation.set_in_layout(False)
        candidates = []
        for dx, dy, ha, va in ((7, 7, "left", "bottom"), (-7, -7, "right", "top"),
                               (7, -7, "left", "top"), (-7, 7, "right", "bottom"),
                               (0, 18, "center", "bottom"), (0, -18, "center", "top"),
                               (7, 32, "left", "bottom"), (-7, 32, "right", "bottom"),
                               (7, -32, "left", "top"), (-7, -32, "right", "top")):
            annotation.set_position((dx, dy))
            annotation.set_ha(ha)
            annotation.set_va(va)
            box = annotation.get_window_extent(renderer).expanded(1.05, 1.15)
            overlap = sum(intersection.width * intersection.height for other in occupied
                          if (intersection := Bbox.intersection(box, other)) is not None)
            overlap += 200 * sum(curve.intersects_bbox(box, filled=False) for curve in curves)
            inside = Bbox.intersection(box, bounds)
            outside = box.width * box.height - (inside.width * inside.height if inside is not None else 0)
            candidates.append((outside * 10 + overlap, dx, dy, ha, va))
        _, dx, dy, ha, va = min(candidates, key=lambda item: item[0])
        annotation.set_position((dx, dy))
        annotation.set_ha(ha)
        annotation.set_va(va)
        occupied.append(annotation.get_window_extent(renderer).expanded(1.05, 1.15))
        if abs(dy) > 18:
            leader = axis.annotate("", (x, y), xytext=(dx, dy), textcoords="offset points",
                                   arrowprops={"arrowstyle": "-", "color": color, "alpha": 0.4,
                                               "linewidth": 0.5})
            leader.set_in_layout(False)
