# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render report-owned RSI milestones and search history from measured chart data."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from matplotlib import font_manager, rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
from matplotlib.ticker import FuncFormatter, MaxNLocator
from matplotlib.transforms import Bbox
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from benchmarks.results.plots.rendering import FigureWriter


_FAILURES = {
    "incorrect": ("Incorrect result", "#e8a33e", "o"),
    "compile_failed": ("Compile failure", "#e45c69", "x"),
    "runtime_failed": ("Runtime failure", "#7a8599", "s"),
}


class _ProgressRecord(BaseModel):
    """Common YAML structure rules for report-owned chart records."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class _Metric(_ProgressRecord):
    """The measured quantity and its improvement direction."""

    name: str
    unit: str
    direction: Literal["higher", "lower"]


class _Baseline(_ProgressRecord):
    """The initial measurement, or an explicitly unmeasured reference."""

    label: str
    value: float | None = Field(gt=0)
    source: str

    @model_validator(mode="after")
    def measured_source(self) -> Self:
        """Require evidence when the baseline becomes measured."""
        if self.value is not None and not self.source:
            raise ValueError("A measured baseline needs a source record")
        return self


class _Point(_ProgressRecord):
    """One attempted evaluation and its optional cumulative-version annotation."""

    x: float = Field(ge=0)
    value: float | None = Field(ge=0)
    status: Literal["valid", "incorrect", "compile_failed", "runtime_failed"]
    source: str = Field(min_length=1)
    label: str = ""
    checkpoint: bool = False
    label_offset: list[float] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def evaluation_outcome(self) -> Self:
        """Keep failed attempts outside the measured curve and cumulative checkpoints."""
        if self.status == "valid":
            if self.value is None:
                raise ValueError("A valid evaluation needs a measured value")
        elif self.value is not None or self.checkpoint:
            raise ValueError("Failed evaluations need value: null and cannot be checkpoints")
        return self


class _ReferencePoint(_ProgressRecord):
    """One measured coordinate of a named comparison trajectory."""

    x: float = Field(ge=0)
    value: float = Field(ge=0)


class _Reference(_ProgressRecord):
    """A named comparison given as a fixed measurement or a measured trajectory."""

    label: str
    source: str = Field(min_length=1)
    value: float | None = Field(default=None, ge=0)
    points: list[_ReferencePoint] | None = None

    @model_validator(mode="after")
    def comparison_values(self) -> Self:
        """Require exactly one complete representation of the comparison measurements."""
        if ("points" in self.model_fields_set) == ("value" in self.model_fields_set):
            raise ValueError("Each reference needs either value or points")
        if "points" in self.model_fields_set:
            if not self.points:
                raise ValueError("A reference trajectory needs measured points")
            self.points.sort(key=lambda point: point.x)
        elif self.value is None:
            raise ValueError("A fixed reference needs a measured value")
        return self


class _Phase(_ProgressRecord):
    """The measured interval covered by one optimization stage."""

    label: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        """Require a stage's end to follow its start."""
        if self.end <= self.start:
            raise ValueError("Phase bounds require start < end")
        return self


class _Progress(_ProgressRecord):
    """One comparable report condition, its evidence, and its plotting annotations."""

    title: str
    subtitle: str
    updated_at: str
    x_label: str
    metric: _Metric
    baseline: _Baseline
    points: list[_Point]
    phases: list[_Phase] = Field(default_factory=list)
    references: list[_Reference] = Field(default_factory=list)

    @field_validator("title", "subtitle", "updated_at", "x_label")
    @classmethod
    def described_report(cls, value: str) -> str:
        """Require the chart's identifying text without changing the writer's wording."""
        if not value.strip():
            raise ValueError("Report descriptions must not be empty")
        return value

    @model_validator(mode="after")
    def measurement_values(self) -> Self:
        """Order attempts and reject zero denominators for lower-is-better metrics."""
        if self.metric.direction == "lower":
            values = [point.value for point in self.points if point.status == "valid"]
            for reference in self.references:
                if reference.points is not None:
                    values.extend(point.value for point in reference.points)
                else:
                    values.append(reference.value)
            if any(value == 0 for value in values):
                raise ValueError("Lower-is-better measurements must be positive")
        self.points.sort(key=lambda point: point.x)
        return self


def load_progress(path: Path) -> dict[str, Any]:
    """Load one writer-maintained progress YAML for the shared offline figure exporter."""
    try:
        progress = _Progress.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"Invalid progress file {path}: {error}") from error
    return progress.model_dump(exclude_unset=True)


def _gain(value: float, data: dict[str, Any]) -> float:
    """Normalize a measured value so larger ratios always mean improvement."""
    baseline = data["baseline"]["value"]
    return value / baseline if data["metric"]["direction"] == "higher" else baseline / value


def _evidence_link(source: str, path: Path, directory: Path) -> str:
    """Resolve local evidence from its YAML location while keeping exported links relative."""
    parts = urlsplit(source)
    if parts.scheme or parts.netloc or not parts.path:
        return source
    relative = Path(os.path.relpath(path.parent / unquote(parts.path), directory)).as_posix()
    return urlunsplit(("", "", quote(relative, safe="/"), parts.query, parts.fragment))


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
                                                  "fontfamily": "DejaVu Sans", "weight": "bold",
                                                  "url": point["source"]}),
            TextArea(_wrap(point["label"], 145, 8, renderer),
                     textprops={"color": "#666c78", "fontsize": 8, "multialignment": "center",
                                "url": point["source"]}),
        ], align="center", pad=0, sep=2)
        label.set_figure(figure)
        extent = label.get_bbox(renderer)
        width, height = extent.width + 10, extent.height + 8
        x_min, x_max = bounds.x0 + width / 2, bounds.x1 - width / 2
        y_min, y_max = bounds.y0 + height / 2, bounds.y1 - height / 2
        if point.get("label_offset") is not None:
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
        axis.add_artist(annotation)


def _plot_search(
    axis: Any, data: dict[str, Any], valid: list[dict[str, Any]], color: str, last_x: float,
) -> tuple[list[float], list[dict[str, Any]]]:
    """Draw measured attempts, their running best and named comparison trajectories."""
    ratios = [_gain(point["value"], data) for point in valid]
    xs = [point["x"] for point in valid]
    best, peak = [1.0], 1.0
    peak_source = data["baseline"]["source"]
    for point, ratio in zip(valid, ratios):
        if ratio >= peak:
            peak = ratio
            peak_source = point["source"]
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
                      fontsize=9, fontfamily="DejaVu Sans", weight="bold", url=peak_source)
        if endpoint_label:
            axis.annotate(endpoint_label, (last_x, peak), xytext=(-2, 7), textcoords="offset points",
                          ha="right", va="bottom", color="#666c78", fontsize=8, url=valid[-1]["source"])
            labeled = valid[:-1]
    axis.annotate(f"{data['baseline']['label']} 1×", (last_x, 1), xytext=(-2, -5),
                  textcoords="offset points", ha="right", va="top", color="#8c909b", fontsize=8,
                  url=data["baseline"]["source"])
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


def render_progress(data: dict[str, Any], path: Path, writer: FigureWriter) -> None:
    """Draw report milestones and search history through the exporter's shared figure writer."""
    # Evidence paths belong to the input document, not the selected export directory.
    data = {**data}
    for key in ("points", "references"):
        data[key] = [{**record, "source": _evidence_link(record["source"], path, writer.directory)}
                     for record in data.get(key, [])]
    data["baseline"] = {**data["baseline"],
                        "source": _evidence_link(data["baseline"]["source"], path, writer.directory)}
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
    with rc_context(style):
        for view in ("milestones", "search"):
            search = view == "search"
            history = [*data["points"], *(reference_points if search else [])]
            last_x = max((point["x"] for point in history), default=0)
            width = last_x or 1
            color = "#635bdf" if search else "#3278ce"
            figure = Figure(figsize=(14, 5.7), facecolor="white")
            FigureCanvasAgg(figure)
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
            writer.figure(figure, f"{path.stem}-{view}",
                          description=f"Updated {data['updated_at']}; data: {path.name}")
