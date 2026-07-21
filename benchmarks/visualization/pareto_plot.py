from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import matplotlib.pyplot as plt

from benchmarks.analyzer.pareto import pareto_xy
from benchmarks.analyzer.vllm_compat import plot_vllm_pareto, vllm_pareto_available


def _plot_with_matplotlib(
    results: list[dict[str, Any]],
    output: str,
    *,
    frontier: Optional[list[dict[str, Any]]],
    title: Optional[str] = None,
) -> str:
    plt.figure(figsize=(8, 6))

    if results:
        xs = [pareto_xy(r)[0] for r in results]
        ys = [pareto_xy(r)[1] for r in results]
        plt.scatter(xs, ys, c="0.5", alpha=0.6, label="All runs")
        for r in results:
            x, y = pareto_xy(r)
            label = r.get("label") or r.get("combination") or r.get("parallel", "")
            plt.annotate(str(label), (x, y), fontsize=8)

    if frontier:
        ordered = sorted(frontier, key=lambda r: pareto_xy(r)[0])
        fxs = [pareto_xy(r)[0] for r in ordered]
        fys = [pareto_xy(r)[1] for r in ordered]
        plt.plot(
            fxs,
            fys,
            marker="o",
            color="#c0392b",
            label="Pareto frontier",
        )

    plt.xlabel("Tokens/s/user")
    plt.ylabel("Tokens/s/GPU")
    plt.title(title or "Foretoken Sweep Pareto")
    plt.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
    if frontier or results:
        plt.legend(loc="best")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output, dpi=300, bbox_inches="tight")
    plt.close()
    return output


def plot_pareto(
    results: list[dict[str, Any]],
    output: str,
    *,
    frontier: Optional[list[dict[str, Any]]] = None,
    gpu_count: int = 1,
    label_by: Optional[list[str]] = None,
    title: Optional[str] = None,
) -> str:
    """Plot tokens/s/user vs tokens/s/GPU Pareto.

    Reuses ``vllm.benchmarks.sweep.plot_pareto._plot_fig`` when available.
    """
    if vllm_pareto_available():
        return str(
            plot_vllm_pareto(
                results,
                output,
                gpu_count=gpu_count,
                label_by=label_by,
            )
        )

    print(
        "[pareto] vLLM not available; using local matplotlib plot "
        "(install vllm + seaborn to reuse official plot_pareto)"
    )
    return _plot_with_matplotlib(
        results,
        output,
        frontier=frontier,
        title=title,
    )
