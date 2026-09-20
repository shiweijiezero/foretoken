# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Plot the Pareto frontier for an HTTP benchmark parameter sweep."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def _pareto_coordinates(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build scatter data when both normalized throughput coordinates are available."""
    throughput = item["throughput"]
    per_concurrency = throughput.get(
        "generation_tokens_per_second_per_configured_concurrency"
    )
    per_gpu = throughput.get("generation_tokens_per_second_per_gpu")
    if per_concurrency is None or per_gpu is None:
        return None
    return {
        "param_group": str(item["parameter_group"]),
        "configured_concurrency": int(item["parallel"]),
        "generation_tokens_per_second_per_configured_concurrency": float(
            per_concurrency
        ),
        "generation_tokens_per_second_per_gpu": float(per_gpu),
    }


def _pareto_frontier(
    points: list[dict[str, Any]],
    *,
    epsilon: float = 1e-9,
) -> list[dict[str, Any]]:
    """Return points not dominated on per-concurrency and per-GPU output throughput."""
    ordered = sorted(
        points,
        key=lambda row: (
            -float(row["generation_tokens_per_second_per_configured_concurrency"]),
            -float(row["generation_tokens_per_second_per_gpu"]),
        ),
    )
    frontier: list[dict[str, Any]] = []
    best_y = -math.inf
    for row in ordered:
        y_val = float(row["generation_tokens_per_second_per_gpu"])
        if y_val > best_y + epsilon:
            frontier.append(row)
            best_y = y_val
    frontier.sort(
        key=lambda row: float(
            row["generation_tokens_per_second_per_configured_concurrency"]
        )
    )
    return frontier


def _marker_area(configured_concurrency: float) -> float:
    """Compute scatter area from concurrency."""
    return 36.0 + 18.0 * configured_concurrency


def _plot_pareto_scatter(fig_path: Path, points: list[dict[str, Any]]) -> None:
    """Color points by parameter group and size them by concurrency."""
    import matplotlib.pyplot as plt

    groups = sorted({str(row["param_group"]) for row in points})
    cmap = plt.get_cmap("tab10")
    group_color = {
        group: cmap(index % 10) for index, group in enumerate(groups)
    }

    fig, ax = plt.subplots()
    for group in groups:
        rows = [row for row in points if str(row["param_group"]) == group]
        ax.scatter(
            [
                float(
                    row[
                        "generation_tokens_per_second_per_configured_concurrency"
                    ]
                )
                for row in rows
            ],
            [float(row["generation_tokens_per_second_per_gpu"]) for row in rows],
            s=[_marker_area(float(row["configured_concurrency"])) for row in rows],
            color=group_color[group],
            alpha=0.75,
            edgecolors="white",
            linewidths=0.5,
            label=group,
            zorder=2,
        )

    frontier = _pareto_frontier(points)
    if len(frontier) >= 2:
        ax.plot(
            [
                float(
                    row[
                        "generation_tokens_per_second_per_configured_concurrency"
                    ]
                )
                for row in frontier
            ],
            [float(row["generation_tokens_per_second_per_gpu"]) for row in frontier],
            color="0.2",
            linewidth=1.2,
            marker="o",
            markersize=3,
            label="Pareto frontier",
            zorder=3,
        )

    ax.set_xlabel(
        "Output token throughput per configured concurrency\n(tokens/s)"
    )
    ax.set_ylabel("Output token throughput per GPU (tokens/s)")
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
    ax.legend(
        title="Parameter group",
        fontsize=8,
        title_fontsize=9,
        framealpha=0.9,
    )
    fig.tight_layout()
    fig.savefig(fig_path)
    plt.close(fig)


def plot_sweep_pareto(
    results: list[dict[str, Any]],
    output_dir: str | Path,
) -> Path | None:
    """Plot points with both normalized throughputs, or return None when none qualify.

    Colors represent parameter combinations and point size represents concurrency;
    write the result to ``output_dir/pareto/PARETO.png``.
    """
    points = [
        point
        for item in results
        if (point := _pareto_coordinates(item)) is not None
    ]
    if not points:
        return None

    out = Path(output_dir)
    fig_dir = out / "pareto"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig_path = fig_dir / "PARETO.png"
    _plot_pareto_scatter(fig_path, points)
    if not fig_path.is_file():
        raise FileNotFoundError(f"Pareto plot not written: {fig_path}")
    return fig_path
