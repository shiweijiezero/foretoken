# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Pareto plot for load-sweep results (Tok/s/user vs Tok/s/GPU)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from vllm.benchmarks.sweep.plot_pareto import (
    _pareto_frontier,
    _plot_fig,
    _prepare_records,
)


def to_vllm_records(results: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Flatten sweep metrics into Pareto run records."""
    records: list[dict[str, object]] = []
    for index, item in enumerate(results):
        parallel = int(item["parallel"])
        # Open-loop stores parallel=-1; Pareto X needs a positive user count.
        user_count = 1 if parallel < 0 else parallel
        if user_count < 1:
            raise ValueError(
                f"invalid parallel={parallel} for Pareto user count"
            )
        item_gpus = int(item["gpu_count"])
        if item_gpus < 1:
            raise ValueError(f"gpu_count must be >= 1, got {item_gpus}")
        records.append(
            {
                "output_throughput": float(item["throughput"]["token/s"]),
                "max_concurrency": user_count,
                "gpu_count": item_gpus,
                "parallel": parallel,
                "number": item["number"],
                "rate": item["rate"],
                "_foretoken_index": index,
            }
        )
    return records


def plot_load_sweep_pareto(
    results: list[dict[str, Any]],
    output_dir: str | Path,
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    """Plot Tok/s/user vs Tok/s/GPU for a load sweep.

    Writes ``summary.json`` and ``pareto/PARETO.png`` under ``output_dir``.
    Returns ``(fig_path, prepared_rows, frontier_rows)``.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    records = to_vllm_records(results)
    summary_path = out / "summary.json"
    with summary_path.open("w", encoding="utf-8") as file:
        json.dump(records, file, indent=4, ensure_ascii=False)

    prepared, skipped = _prepare_records(
        records,
        user_count_var="max_concurrency",
        gpu_count_var="gpu_count",
    )
    if skipped:
        raise ValueError(
            f"Pareto skipped {skipped} runs without user count; "
            "refusing incomplete plot"
        )
    if not prepared:
        raise ValueError(
            "No data points with throughput and user count for Pareto plot"
        )

    fig_dir = out / "pareto"
    fig_dir.mkdir(parents=True, exist_ok=True)
    _plot_fig(
        fig_dir,
        (tuple(), prepared),
        label_by=["max_concurrency", "gpu_count"],
        dry_run=False,
    )
    fig_path = fig_dir / "PARETO.png"
    if not fig_path.is_file():
        raise FileNotFoundError(f"Pareto plot not written: {fig_path}")

    frame = pd.DataFrame.from_records(prepared)
    frontier_frame = _pareto_frontier(
        frame, "tokens_per_user", "tokens_per_gpu"
    ).sort_values("tokens_per_user")
    frontier = frontier_frame.to_dict(orient="records")
    return fig_path, prepared, frontier
