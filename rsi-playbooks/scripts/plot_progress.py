# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render report-owned RSI milestones and search history from measured chart data."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
from matplotlib.ticker import FuncFormatter, MaxNLocator
from matplotlib.transforms import Bbox
import yaml


_FAILURES = {
    "incorrect": ("Incorrect result", "#e8a33e", "o"),
    "compile_failed": ("Compile failure", "#e45c69", "x"),
    "runtime_failed": ("Runtime failure", "#7a8599", "s"),
}


def load_progress(path: Path) -> dict[str, Any]:
    """Read writer-maintained data, rejecting values that would misstate normalized gains."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    direction = data["metric"]["direction"]
    if direction not in {"higher", "lower"}:
        raise ValueError("metric.direction must be higher or lower")
    for field in ("title", "subtitle", "updated_at", "x_label"):
        if not str(data[field]).strip():
            raise ValueError(f"{field} must describe this report")
    baseline = data["baseline"]
    measured = [baseline] if baseline["value"] is not None else []
    if measured and baseline["value"] <= 0:
        raise ValueError("baseline.value must be positive or null when unmeasured")
    for point in data["points"]:
        if not math.isfinite(point["x"]) or point["x"] < 0:
            raise ValueError("point.x must be finite and nonnegative")
        if point["status"] not in {"valid", *_FAILURES}:
            raise ValueError(f"Unknown point status: {point['status']}")
        if not point["source"]:
            raise ValueError("Each point needs a source record")
        if point["status"] == "valid":
            measured.append(point)
        elif point["value"] is not None or point.get("checkpoint", False):
            raise ValueError("Failed evaluations need value: null and cannot be checkpoints")
    for reference in data.get("references", []):
        if ("points" in reference) == ("value" in reference):
            raise ValueError("Each reference needs either value or points")
        if "points" in reference:
            if not reference["points"]:
                raise ValueError("A reference trajectory needs measured points")
            for point in reference["points"]:
                if not math.isfinite(point["x"]) or point["x"] < 0:
                    raise ValueError("Reference point.x must be finite and nonnegative")
                measured.append({**point, "source": reference["source"]})
            reference["points"].sort(key=lambda point: point["x"])
        else:
            measured.append(reference)
    for record in measured:
        value = record["value"]
        if value is None or not math.isfinite(value) or value < 0 or (direction == "lower" and value == 0):
            raise ValueError("Measured values must be finite and nonnegative; latency must be positive")
        if not record["source"]:
            raise ValueError("Each measured value needs a source record")
    for phase in data.get("phases", []):
        if not 0 <= phase["start"] < phase["end"] or not math.isfinite(phase["end"]):
            raise ValueError("Phase bounds must be finite with 0 <= start < end")
    data["points"].sort(key=lambda point: point["x"])
    return data


def _gain(value: float, data: dict[str, Any]) -> float:
    """Normalize a measured value so larger ratios always mean improvement."""
    baseline = data["baseline"]["value"]
    return value / baseline if data["metric"]["direction"] == "higher" else baseline / value


def _wrap(text: str, width: float, size: float, renderer: Any) -> str:
    """Wrap chart text by rendered width, including Chinese text without spaces."""
    font = FontProperties(size=size)
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for character in paragraph:
            candidate = line + character
            if line and renderer.get_text_width_height_descent(candidate, font, False)[0] > width:
                split = line.rfind(" ")
                if split > len(line) / 2 and not character.isspace():
                    lines.append(line[:split])
                    line = line[split + 1:] + character
                else:
                    lines.append(line.rstrip())
                    line = character.lstrip()
            else:
                line = candidate
        lines.append(line.rstrip())
    return "\n".join(lines)


def _ratio_label(value: float) -> str:
    """Keep small measured gains visible in annotations and comparison labels."""
    return f"{value:.4f}".rstrip("0").rstrip(".") + "×"


def _annotate(axis: Any, points: list[dict[str, Any]], data: dict[str, Any], color: str) -> None:
    """Place evidence labels in available plot space using their rendered text bounds."""
    figure = axis.figure
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    bounds = axis.get_window_extent().padded(-8)
    occupied = [text.get_window_extent(renderer).padded(5) for text in axis.texts]
    markers = axis.transData.transform([(point["x"], _gain(point["value"], data)) for point in points])
    for point, anchor in zip(points, markers):
        if not point.get("label"):
            continue
        ratio = _gain(point["value"], data)
        label = VPacker(children=[
            TextArea(_ratio_label(ratio), textprops={"color": color, "fontsize": 9,
                                                  "fontfamily": "DejaVu Sans", "weight": "bold"}),
            TextArea(_wrap(point["label"], 145, 8, renderer),
                     textprops={"color": "#666c78", "fontsize": 8, "multialignment": "center"}),
        ], align="center", pad=0, sep=2)
        label.set_figure(figure)
        extent = label.get_bbox(renderer)
        width, height = extent.width + 10, extent.height + 8
        x_min, x_max = bounds.x0 + width / 2, bounds.x1 - width / 2
        y_min, y_max = bounds.y0 + height / 2, bounds.y1 - height / 2
        if "label_offset" in point:
            dx, dy = point["label_offset"]
            candidates = [(anchor[0] + dx * figure.dpi / 72, anchor[1] + dy * figure.dpi / 72)]
        else:
            # Nearby placements come first; a plot-wide grid also accommodates clustered milestones.
            candidates = [(anchor[0], anchor[1] + sign * (height / 2 + 22)) for sign in (1, -1)]
            columns = max(1, int(bounds.width / (width / 2)))
            rows = max(1, int(bounds.height / (height / 2)))
            candidates.extend((x_min + (x_max - x_min) * col / columns,
                               y_min + (y_max - y_min) * row / rows)
                              for col in range(columns + 1) for row in range(rows + 1))
        placements = []
        for x, y in candidates:
            x, y = min(max(x, x_min), x_max), min(max(y, y_min), y_max)
            box = Bbox.from_bounds(x - width / 2, y - height / 2, width, height)
            collisions = sum(box.overlaps(other) for other in occupied)
            covered = sum(box.contains(*marker) for marker in markers)
            distance = (x - anchor[0]) ** 2 + (y - anchor[1]) ** 2
            placements.append(((collisions, covered, distance), box, (x, y)))
        _, box, position = min(placements, key=lambda placement: placement[0])
        occupied.append(box)
        annotation = AnnotationBbox(
            label, (point["x"], ratio), xybox=axis.transAxes.inverted().transform(position),
            boxcoords=axis.transAxes, frameon=False,
            arrowprops={"arrowstyle": "-", "color": color, "alpha": 0.22, "lw": 0.6}, zorder=6,
        )
        annotation.set_url(point["source"])
        axis.add_artist(annotation)


def _plot_search(
    axis: Any, data: dict[str, Any], valid: list[dict[str, Any]], color: str, last_x: float,
) -> tuple[list[float], list[dict[str, Any]]]:
    """Draw measured attempts, their running best and named comparison trajectories."""
    ratios = [_gain(point["value"], data) for point in valid]
    xs = [point["x"] for point in valid]
    best, peak = [1.0], 1.0
    for ratio in ratios:
        peak = max(peak, ratio)
        best.append(peak)
    axis.step([0, *xs, last_x], [*best, best[-1]], where="post",
              color=color, lw=1.6, label="Best so far", zorder=4)
    labeled = valid
    if valid:
        axis.scatter(xs, ratios, color=color, alpha=0.30, s=12, label="Valid evaluation", zorder=3)
        named = [(point["x"], ratio) for point, ratio in zip(valid, ratios) if point.get("label")]
        if named:
            axis.scatter(*zip(*named), s=29, facecolors="white", edgecolors=color, linewidths=1.1, zorder=5)
        endpoint_label = valid[-1].get("label") if xs[-1] == last_x and ratios[-1] == peak else None
        axis.annotate(f"Foretoken {_ratio_label(peak)}", (last_x, peak), xytext=(-2, 22 if endpoint_label else 7),
                      textcoords="offset points", ha="right", va="bottom", color=color,
                      fontsize=9, fontfamily="DejaVu Sans", weight="bold")
        if endpoint_label:
            axis.annotate(endpoint_label, (last_x, peak), xytext=(-2, 7), textcoords="offset points",
                          ha="right", va="bottom", color="#666c78", fontsize=8)
            labeled = valid[:-1]
    axis.annotate(f"{data['baseline']['label']} 1×", (last_x, 1), xytext=(-2, -5),
                  textcoords="offset points", ha="right", va="top", color="#8c909b", fontsize=8)
    palette = ("#9c9da8", "#b0a8dd", "#8792c9", "#b9bbc3")
    for index, reference in enumerate(data.get("references", [])):
        reference_color = palette[index % len(palette)]
        if "points" in reference:
            rx = [point["x"] for point in reference["points"]]
            ry = [_gain(point["value"], data) for point in reference["points"]]
            axis.plot(rx, ry, color=reference_color, lw=1.1, url=reference["source"])
            end_x, end_y = rx[-1], ry[-1]
            ratios.extend(ry)
        else:
            end_x, end_y = last_x, _gain(reference["value"], data)
            axis.axhline(end_y, color=reference_color, ls="--", lw=0.9, url=reference["source"])
            ratios.append(end_y)
        axis.annotate(f"{reference['label']} {_ratio_label(end_y)}", (end_x, end_y), xytext=(-2, 4),
                      textcoords="offset points", ha="right", va="bottom", color=reference_color,
                      fontsize=8, url=reference["source"])
    return ratios, labeled


def render_progress(data: dict[str, Any], path: Path) -> list[Path]:
    """Replace both PNG/SVG views beside the input for the report's single writer."""
    files = []
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    style = {
        "font.family": [name for name in ("DejaVu Sans", "Noto Sans CJK SC", "Source Han Sans SC",
                                          "PingFang SC", "Microsoft YaHei", "Arial Unicode MS")
                        if name in available_fonts],
        "font.size": 9,
        "axes.labelcolor": "#647086",
        "xtick.color": "#7c879b", "ytick.color": "#7c879b",
    }
    baseline = data["baseline"]
    valid = [point for point in data["points"] if point["status"] == "valid"]
    reference_points = [point for reference in data.get("references", []) for point in reference.get("points", [])]
    with plt.rc_context(style):
        for view in ("milestones", "search"):
            search = view == "search"
            history = [*data["points"], *(reference_points if search else [])]
            last_x = max((point["x"] for point in history), default=0)
            width = last_x or 1
            color = "#635bdf" if search else "#3278ce"
            figure = plt.figure(figsize=(14, 5.7), facecolor="white")
            renderer = figure.canvas.get_renderer()
            title = data["title"] if data["title"].lower().startswith("foretoken") else f"Foretoken · {data['title']}"
            title = _wrap(title, 12.1 * figure.dpi, 14, renderer)
            subtitle = _wrap(data["subtitle"], 12.1 * figure.dpi, 8, renderer)
            title_height = 0.24 * len(title.splitlines())
            header = 0.23 + title_height + 0.17 * len(subtitle.splitlines())
            footer = 0.75 if search else 0.50
            height = header + 4.0 + footer
            figure.set_size_inches(14, height)
            axis = figure.add_axes((0.07, footer / height, 0.90, 4.0 / height))
            axis.set_facecolor("#faf9ff" if search else "#f7faff")
            for spine in axis.spines.values():
                spine.set_visible(False)
            axis.grid(axis="both" if search else "y", color="#e0e4ed", alpha=0.5, lw=0.55)
            axis.set_axisbelow(True)
            axis.tick_params(length=0, pad=5, labelsize=8)
            axis.set_xlim(-0.035 * width, 1.025 * width)
            axis.xaxis.set_major_locator(MaxNLocator(nbins=10))
            axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}×"))
            axis.set_ylabel(data["metric"]["name"] + " / baseline" if data["metric"]["direction"] == "higher"
                            else "Baseline / " + data["metric"]["name"], labelpad=14)
            if not search:
                axis.set_xlabel(data["x_label"], labelpad=12)

            if not search:
                for index, phase in enumerate(data.get("phases", [])):
                    end = min(phase["end"], last_x)
                    if end <= phase["start"]:
                        continue
                    axis.axvspan(phase["start"], end, color=("#e8f0fb", "#deebf9", "#e1f0f1")[index % 3], alpha=0.75)
                    phase_width = axis.bbox.width * (end - phase["start"]) / (1.06 * width)
                    axis.text((phase["start"] + end) / 2, 0.97,
                              _wrap(phase["label"], max(30, phase_width - 16), 10, renderer),
                              transform=axis.get_xaxis_transform(), ha="center", va="top",
                              color="#48566b", fontsize=10)

            ratios = []
            if baseline["value"] is None:
                axis.set_ylim(0, 2)
                axis.text(0.5, 0.48, "Awaiting baseline measurement", transform=axis.transAxes,
                          ha="center", color="#7c879b", fontsize=15)
            else:
                axis.axhline(1, color="#999ca6", ls="--", lw=0.85)
                baseline_point = {"x": 0, "label": "" if search else baseline["label"], "source": baseline["source"],
                                  "value": baseline["value"]}
                if search:
                    ratios, labeled = _plot_search(axis, data, valid, color, last_x)
                    annotations = [baseline_point, *labeled]
                else:
                    checkpoints = [baseline_point,
                                   *(point for point in valid if point.get("checkpoint", False))]
                    xs = [point["x"] for point in checkpoints]
                    ratios = [_gain(point["value"], data) for point in checkpoints]
                    axis.plot(xs, ratios, color=color, lw=1.6, marker="o", ms=3.5, zorder=3)
                    axis.fill_between(xs, 1, ratios, color=color, alpha=0.07)
                    ticks = xs[::max(1, math.ceil(len(xs) / 14))]
                    axis.set_xticks(sorted(set([*ticks, xs[-1]])))
                    annotations = checkpoints
                low, high = min([1, *ratios]), max([1, *ratios])
                span = high - low or 0.05
                axis.set_ylim(max(0, low - span * 0.10), high + span * (0.18 if search else 0.30))
                _annotate(axis, annotations, data, color if search else "#268f99")

            if search:
                failures = figure.add_axes((0.07, (footer - 0.04) / height, 0.90, 0.08 / height))
                failures.set_xlim(axis.get_xlim())
                failures.set_ylim(-1, 1)
                failures.set_axis_off()
                for status, (label, failure_color, marker) in _FAILURES.items():
                    xs = [point["x"] for point in data["points"] if point["status"] == status]
                    if xs:
                        failures.scatter(xs, [0] * len(xs), color=failure_color, marker=marker, s=16, label=label)
                figure.text(0.52, 0.34 / height, data["x_label"], ha="center", color="#647086", fontsize=9)
                handles, labels = axis.get_legend_handles_labels()
                failure_handles, failure_labels = failures.get_legend_handles_labels()
                handles, labels = [*handles, *failure_handles], [*labels, *failure_labels]
                if handles:
                    figure.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.52, 0.01 / height),
                                  frameon=False, ncol=5, fontsize=8)

            figure.text(0.07, 1 - 0.08 / height, title, fontsize=14, color="#303642", va="top")
            figure.text(0.07, 1 - (0.14 + title_height) / height, subtitle, fontsize=8,
                        color="#647086", linespacing=1.4, va="top")
            for extension in ("png", "svg"):
                output = path.with_name(f"{path.stem}-{view}.{extension}")
                figure.savefig(output, dpi=180, bbox_inches="tight", facecolor="white",
                               metadata={"Description": f"Updated {data['updated_at']}; data: {path.name}"})
                files.append(output)
            plt.close(figure)
    return files


def main() -> None:
    """Render each report data file passed by its writer and print the resulting paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+", help="Report progress YAML files, one per comparable condition")
    args = parser.parse_args()
    for path in args.inputs:
        for output in render_progress(load_progress(path), path):
            print(output)


if __name__ == "__main__":
    main()
