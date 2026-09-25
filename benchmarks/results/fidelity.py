# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present and publish paired next-token distribution fidelity measurements."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

import wandb

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.results.evaluation import evaluation_sinks, publish_quality_wandb
from benchmarks.results.output import BenchmarkRun, ResultSink

logger = logging.getLogger(__name__)

_PALETTE = ("#2a78d6", "#eb6834", "#1baf7a")
_COORDINATE_LABELS = {
    "weight_bits": "Nominal weight precision (bits)",
    "bits_per_weight": "Effective bits per weight",
    "model_size_gib": "Checkpoint size (GiB)",
}
_METRIC_COLUMNS = (
    ("mean_kl", "Mean KL (nats)"),
    ("p99_kl", "p99 KL (nats)"),
    ("mean_centered_logit_rmse", "Centered-logit RMSE"),
    ("top1_agreement", "Top-1 agreement (%)"),
)
_POINT_COLUMNS = (
    "model",
    "label",
    "method",
    "weight_bits",
    "bits_per_weight",
    "model_size_gib",
    "scored_positions",
    "vocab_size",
    "mean_kl",
    "median_kl",
    "p99_kl",
    "top1_agreement",
    "reference_token_mean_delta_p",
    "reference_token_rms_delta_p",
    "mean_centered_logit_rmse",
    "mean_total_variation",
)
_POSITION_COLUMNS = (
    "candidate",
    "window",
    "position",
    "scored_index",
    "kl",
    "top1_match",
    "reference_token_delta_p",
    "centered_logit_rmse",
    "total_variation",
)


def _columns(protocol: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Add exactly the top-k overlap columns requested by the scoring protocol."""
    top_k = tuple(protocol["top_k"])
    point_columns = _POINT_COLUMNS + tuple(f"top{k}_overlap" for k in top_k)
    position_columns = _POSITION_COLUMNS + tuple(f"top{k}_overlap" for k in top_k)
    return point_columns, position_columns


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write a stable tabular projection for local inspection and downstream import."""
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row[column] for column in columns})


def _method_colors(points: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """Assign the validated first three method slots and collapse later methods to Other."""
    methods = list(dict.fromkeys(point["method"] for point in points))
    colors = {
        method: _PALETTE[index] if index < len(_PALETTE) else "#898781"
        for index, method in enumerate(methods)
    }
    legend_methods = methods[: len(_PALETTE)]
    if len(methods) > len(_PALETTE):
        legend_methods.append("Other methods")
    return colors, legend_methods


def _method_handles(methods: list[str], colors: dict[str, str]) -> list[Any]:
    """Build the method legend handles used by every candidate comparison figure."""
    from matplotlib.patches import Patch

    return [
        Patch(facecolor=colors.get(method, "#898781"), label=method)
        for method in methods
    ]


def _plot_candidate_panels(
    path: Path,
    points: list[dict[str, Any]],
    x_field: str | None,
    title: str,
) -> bool:
    """Render four candidate comparisons against one explicitly named coordinate."""
    import matplotlib.pyplot as plt

    rows = [point for point in points if point[x_field] is not None] if x_field else points
    fields = [
        (field, name)
        for field, name in _METRIC_COLUMNS
        if any(point[field] is not None for point in rows)
    ]
    if not rows or not fields:
        return False
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharex=True)
    colors, methods = _method_colors(points)
    for index, axis in enumerate(axes.flat):
        if index >= len(fields):
            axis.axis("off")
            continue
        field, metric_name = fields[index]
        plotted = [point for point in rows if point[field] is not None]
        xs = (
            [float(point[x_field]) for point in plotted]
            if x_field
            else list(range(len(plotted)))
        )
        if x_field:
            axis.set_xlabel(_COORDINATE_LABELS[x_field])
        else:
            axis.set_xlabel("Candidate")
            axis.set_xticks(xs, [point["label"] for point in plotted], rotation=25, ha="right")
        for point, x in zip(plotted, xs):
            value = float(point[field]) * 100 if field == "top1_agreement" else float(point[field])
            axis.scatter(
                x,
                value,
                s=58,
                color=colors[point["method"]],
                edgecolors="#fcfcfb",
                linewidths=2,
                zorder=3,
            )
            axis.annotate(
                point["label"],
                (x, value),
                xytext=(-6 if x == max(xs) else 6, 6),
                ha="right" if x == max(xs) else "left",
                textcoords="offset points",
                fontsize=8,
            )
        if field in {"mean_kl", "p99_kl"}:
            axis.set_yscale("symlog", linthresh=1e-6)
        axis.set_ylabel(metric_name)
        axis.grid(True, color="#e1e0d9", linewidth=0.7)
        axis.spines[["top", "right"]].set_visible(False)
    if x_field:
        _plot_frontier(axes.flat[0], rows, x_field)
    axes.flat[0].legend(
        handles=_method_handles(methods, colors) + axes.flat[0].get_legend_handles_labels()[0],
        fontsize=8,
        frameon=False,
    )
    fig.suptitle(title, x=0.06, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="#fcfcfb")
    plt.close(fig)
    return True


def _plot_frontier(axis: Any, rows: list[dict[str, Any]], x_field: str) -> None:
    """Overlay the lower-cost/lower-KL frontier without changing plotted measurements."""
    eligible = [
        row for row in rows
        if row[x_field] is not None and row["mean_kl"] is not None
    ]
    frontier: list[dict[str, Any]] = []
    for row in sorted(eligible, key=lambda item: (float(item[x_field]), float(item["mean_kl"]))):
        if not frontier or float(row["mean_kl"]) < float(frontier[-1]["mean_kl"]):
            frontier.append(row)
    if len(frontier) >= 2:
        axis.plot(
            [float(row[x_field]) for row in frontier],
            [float(row["mean_kl"]) for row in frontier],
            color="#52514e",
            linestyle="--",
            linewidth=1.3,
            marker="o",
            markersize=4,
            label="min-cost / min-KL frontier",
            zorder=2,
        )


def _plot_position_curves(
    path: Path,
    points: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> bool:
    """Plot KL and centered-logit RMSE by scored position index, never by elapsed time."""
    import matplotlib.pyplot as plt

    point_by_label = {point["label"]: point for point in points}
    candidates = list(point_by_label)
    if not positions:
        return False
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    colors, methods = _method_colors(points)
    for candidate in candidates:
        rows = [row for row in positions if row["candidate"] == candidate]
        for axis, field in (
            (axes[0], "kl"),
            (axes[1], "centered_logit_rmse"),
        ):
            plotted = [row for row in rows if row[field] is not None]
            if plotted:
                axis.plot(
                    [float(row["scored_index"]) for row in plotted],
                    [float(row[field]) for row in plotted],
                    color=colors[point_by_label[candidate]["method"]],
                    linewidth=2,
                    label=candidate,
                )
    axes[0].set_yscale("symlog", linthresh=1e-6)
    axes[0].set_ylabel("KL divergence (nats)")
    axes[1].set_ylabel("Centered-logit RMSE")
    axes[1].set_xlabel("Scored position index (not time)")
    for axis in axes:
        axis.grid(True, color="#e1e0d9", linewidth=0.7)
        axis.spines[["top", "right"]].set_visible(False)
    axes[0].legend(
        handles=_method_handles(methods, colors),
        title="Method",
        fontsize=8,
        title_fontsize=8,
        frameon=False,
    )
    axes[1].legend(title="Candidate", fontsize=8, frameon=False, ncol=2)
    fig.suptitle("Fidelity across scored token positions", x=0.06, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="#fcfcfb")
    plt.close(fig)
    return True


def write_fidelity_artifacts(directory: str | Path, metrics: dict[str, Any]) -> dict[str, Path]:
    """Write fidelity tables and data-backed PNGs into a result directory."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    fidelity = metrics["fidelity"]
    protocol = fidelity["protocol"]
    points = fidelity["candidates"]
    positions = fidelity["positions"]
    point_columns, _ = _columns(protocol)
    candidate_path = out / "fidelity_candidates.csv"
    token_path = out / "fidelity_tokens.jsonl"
    _write_csv(candidate_path, point_columns, points)
    with token_path.open("w", encoding="utf-8") as stream:
        for row in positions:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    artifacts = {"fidelity_candidates": candidate_path, "fidelity_tokens": token_path}
    numeric_axes = [axis for axis in _COORDINATE_LABELS if any(point[axis] is not None for point in points)]
    if numeric_axes:
        for axis in numeric_axes:
            path = out / f"fidelity_{axis}.png"
            if _plot_candidate_panels(path, points, axis, f"Fidelity: {_COORDINATE_LABELS[axis]}"):
                artifacts[f"fidelity_{axis}_plot"] = path
    else:
        path = out / "fidelity_candidates.png"
        if _plot_candidate_panels(path, points, None, "Fidelity by candidate"):
            artifacts["fidelity_candidates_plot"] = path
    if positions:
        path = out / "fidelity_positions.png"
        if _plot_position_curves(path, points, positions):
            artifacts["fidelity_positions_plot"] = path
    return artifacts


class FidelityArtifactSink:
    """Materialize fidelity tables and figures before downstream publishers consume artifacts."""

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        run.artifacts.update(write_fidelity_artifacts(self.directory, run.metrics))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class FidelityConsoleSink:
    """Print one compact candidate table and protocol summary for a fidelity run."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        fidelity = run.metrics["fidelity"]
        protocol = fidelity["protocol"]
        points = fidelity["candidates"]
        expected = protocol["num_windows"] * protocol["score_tokens"]
        execution = run.metrics["execution"]["fidelity"]
        lines = [
            "Foretoken model comparison",
            f"Reference: {fidelity['reference_model']}    Scored positions/candidate: {expected}    Completed: {execution['succeeded']}/{execution['requested']}",
            "Protocol: " + ", ".join(
                f"{key}={protocol[key]}"
                for key in ("context_length", "num_windows", "score_tokens", "top_k")
            ),
            "",
        ]
        headers = ["Candidate", "Method", "Bits", "Mean KL (nats)", "p99 KL (nats)", "Top-1 (%)", "Centered RMSE"]
        table = [headers]
        for point in points:
            table.append([
                point["label"],
                point["method"],
                _bits_label(point),
                _format(point["mean_kl"]),
                _format(point["p99_kl"]),
                _format(point["top1_agreement"] * 100),
                _format(point["mean_centered_logit_rmse"]),
            ])
        widths = [max(len(str(row[index])) for row in table) for index in range(len(headers))]
        lines.extend("  ".join(str(value).ljust(width) for value, width in zip(row, widths)) for row in table)
        logger.info("\n%s", "\n".join(lines))

    def close(self, *, exit_code: int = 0) -> None:
        return None


def _format(value: Any) -> str:
    """Format a scalar for compact console output."""
    return "—" if value is None else f"{float(value):.4g}"


def _bits_label(point: dict[str, Any]) -> str:
    """Show nominal precision and measured effective bits without conflating them."""
    nominal = point["weight_bits"]
    effective = point["bits_per_weight"]
    if nominal is None and effective is None:
        return "—"
    if nominal is None:
        return f"eff {effective:g}"
    if effective is None:
        return f"nom {nominal:g}"
    return f"{nominal:g}/{effective:g}"


def publish_fidelity_wandb(sdk_run: Any, run: BenchmarkRun) -> None:
    """Publish existing quality output plus fidelity tables, indexed curves, and PNG artifacts."""
    publish_quality_wandb(sdk_run, run)
    fidelity = run.metrics["fidelity"]
    protocol = fidelity["protocol"]
    points = fidelity["candidates"]
    positions = fidelity["positions"]
    point_columns, position_columns = _columns(protocol)
    sdk_run.log({
        "Fidelity/Candidates": wandb.Table(
            columns=list(point_columns),
            data=[[row[column] for column in point_columns] for row in points],
            allow_mixed_types=True,
        ),
    })
    if positions:
        sdk_run.log({
            "Fidelity/Tokens": wandb.Table(
                columns=list(position_columns),
                data=[[row[column] for column in position_columns] for row in positions],
                allow_mixed_types=True,
            ),
        })
        for field, title in (
            ("kl", "KL by scored position index (nats)"),
            ("centered_logit_rmse", "Centered-logit RMSE by scored position index"),
        ):
            series = []
            for point in points:
                label = point["label"]
                rows = [row for row in positions if row["candidate"] == label and row[field] is not None]
                if rows:
                    series.append((label, [float(row["scored_index"]) for row in rows], [float(row[field]) for row in rows]))
            if series:
                sdk_run.log({
                    f"Fidelity/{title}": wandb.plot.line_series(
                        xs=[item[1] for item in series],
                        ys=[item[2] for item in series],
                        keys=[item[0] for item in series],
                        xname="Scored position index (not time)",
                        title=title,
                    ),
                })
    for key, path in run.artifacts.items():
        if path.suffix.lower() == ".png":
            sdk_run.log({f"Fidelity/{path.stem}": wandb.Image(str(path), caption=path.stem)})


def fidelity_sinks(config: EvaluationConfig, record: dict[str, Any], directory: str) -> list[ResultSink]:
    """Compose evaluation publication with fidelity-specific artifacts and presentation."""
    sinks = evaluation_sinks(
        config,
        record,
        directory,
        console_sink=FidelityConsoleSink(),
        publisher=publish_fidelity_wandb,
    )
    return [sinks[0], FidelityArtifactSink(directory), *sinks[1:]]
