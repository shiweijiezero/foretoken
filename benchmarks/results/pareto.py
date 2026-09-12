# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Plot the Pareto frontier for an HTTP benchmark parameter sweep."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from benchmarks.results.metrics import (
    generation_tokens_per_second_per_gpu,
    generation_tokens_per_second_per_user,
    configured_user_denominator,
)


def _pareto_coordinates(item: dict[str, Any]) -> dict[str, Any]:
    """Build scatter data from one sweep metrics dictionary."""
    parallel = int(item["parallel"])
    user_count = configured_user_denominator(parallel)
    gpu_count = int(item["gpu_count"])
    generation_tokens_per_second = float(
        item["throughput"]["generation_tokens_per_second"]
    )
    return {
        "param_group": str(item["parameter_group"]),
        "user_count": user_count,
        "multi_turn": bool(item.get("multi_turn")),
        "generation_tokens_per_second_per_user": generation_tokens_per_second_per_user(
            generation_tokens_per_second, parallel
        ),
        "generation_tokens_per_second_per_gpu": generation_tokens_per_second_per_gpu(
            generation_tokens_per_second, gpu_count
        ),
    }


def _pareto_frontier(
    points: list[dict[str, Any]],
    *,
    epsilon: float = 1e-9,
) -> list[dict[str, Any]]:
    """Return points not dominated on per-user and per-GPU output throughput."""
    ordered = sorted(
        points,
        key=lambda row: (
            -float(row["generation_tokens_per_second_per_user"]),
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
    frontier.sort(key=lambda row: float(row["generation_tokens_per_second_per_user"]))
    return frontier


def _marker_area(user_count: float) -> float:
    """Compute scatter area from concurrency."""
    return 36.0 + 18.0 * user_count


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
            [float(row["generation_tokens_per_second_per_user"]) for row in rows],
            [float(row["generation_tokens_per_second_per_gpu"]) for row in rows],
            s=[_marker_area(float(row["user_count"])) for row in rows],
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
            [float(row["generation_tokens_per_second_per_user"]) for row in frontier],
            [float(row["generation_tokens_per_second_per_gpu"]) for row in frontier],
            color="0.2",
            linewidth=1.2,
            marker="o",
            markersize=3,
            label="Pareto frontier",
            zorder=3,
        )

    denominator = (
        "concurrent conversation"
        if points and all(bool(row["multi_turn"]) for row in points)
        else "user"
    )
    ax.set_xlabel(f"Generation tokens/s/{denominator}")
    ax.set_ylabel("Generation tokens/s/GPU")
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
) -> Path:
    """Plot the Pareto frontier for per-user and per-GPU output throughput.

    Colors represent parameter combinations and point size represents concurrency;
    write the result to ``output_dir/pareto/PARETO.png``.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    points = [_pareto_coordinates(item) for item in results]
    if not points:
        raise ValueError(
            "No data points with throughput and user count for Pareto plot"
        )

    fig_dir = out / "pareto"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig_path = fig_dir / "PARETO.png"
    _plot_pareto_scatter(fig_path, points)
    if not fig_path.is_file():
        raise FileNotFoundError(f"Pareto plot not written: {fig_path}")
    return fig_path
