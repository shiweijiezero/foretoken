"""Adapters to reuse vLLM ``bench sweep`` Pareto helpers when installed.

Optional path only: foretoken can plot Pareto without vLLM via matplotlib.
When ``vllm`` (+ pandas/seaborn) is installed, reuse official helpers for
alignment with vLLM ``plot_pareto``.

vLLM reference (v0.18+):
  ``vllm.benchmarks.sweep.plot_pareto``
  - ``_prepare_records``: tokens/s/user = output_throughput / concurrency
  - ``_pareto_frontier``: maximize-maximize sweep (sorted by x desc)
  - ``_plot_fig``: seaborn scatter + frontier line
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from benchmarks.arguments import metric_parallel
from benchmarks.deps import vllm_plot as vllm_deps


def token_throughput(item: dict[str, Any]) -> float:
    """Resolve tok/s from foretoken / vLLM-shaped result dicts."""
    if item.get("token_s") is not None:
        return float(item["token_s"])
    if item.get("output_throughput") is not None:
        return float(item["output_throughput"])
    return float(item.get("throughput", {}).get("token/s", 0.0) or 0.0)


def vllm_pareto_available() -> bool:
    return vllm_deps.available()


def to_vllm_run_records(
    results: list[dict[str, Any]],
    *,
    gpu_count: int = 1,
) -> list[dict[str, Any]]:
    """Flatten foretoken sweep / param-sweep points into vLLM run records."""
    gpus = max(int(gpu_count), 1)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        throughput = token_throughput(item)
        parallel = max(metric_parallel(item), 1)
        item_gpus = int(item.get("gpu_count") or gpus)
        records.append(
            {
                "output_throughput": throughput,
                "max_concurrency": parallel,
                "max_concurrent_requests": parallel,
                "gpu_count": max(item_gpus, 1),
                "parallel": parallel,
                "number": item.get("number"),
                "combination": item.get("combination"),
                "label": item.get("label"),
                "p99_latency": float(
                    item.get("p99_latency")
                    or item.get("latency", {}).get("p99", 0.0)
                    or 0.0
                ),
                "request_throughput": float(
                    item.get("request_s")
                    or item.get("throughput", {}).get("request/s", 0.0)
                    or 0.0
                ),
                "_foretoken_index": index,
            }
        )
    return records


def prepare_vllm_pareto_rows(
    results: list[dict[str, Any]],
    *,
    gpu_count: int = 1,
) -> list[dict[str, Any]]:
    """Run vLLM ``_prepare_records`` on foretoken sweep results."""
    stack = vllm_deps.require_pareto()
    records = to_vllm_run_records(results, gpu_count=gpu_count)
    prepared, skipped = stack["prepare_records"](
        records,
        user_count_var="max_concurrency",
        gpu_count_var="gpu_count",
    )
    if skipped:
        print(f"[vllm pareto] skipped {skipped} runs without user count")
    return prepared


def vllm_pareto_frontier_indices(
    prepared: list[dict[str, Any]],
) -> list[int]:
    """Return foretoken result indices on the vLLM Pareto frontier."""
    stack = vllm_deps.require_pareto()
    if not prepared:
        return []

    df = stack["pd"].DataFrame.from_records(prepared)
    frontier = stack["pareto_frontier"](df, "tokens_per_user", "tokens_per_gpu")
    frontier = frontier.sort_values("max_concurrency")
    return [int(i) for i in frontier["_foretoken_index"].tolist()]


def plot_vllm_pareto(
    results: list[dict[str, Any]],
    output: str | Path,
    *,
    gpu_count: int = 1,
    label_by: Optional[list[str]] = None,
    dry_run: bool = False,
) -> Path:
    """Plot using vLLM ``_plot_fig`` into ``output`` (no extra result dirs)."""
    import shutil
    import tempfile

    stack = vllm_deps.require_pareto()
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prepared = prepare_vllm_pareto_rows(results, gpu_count=gpu_count)
    if not prepared:
        raise ValueError("No points available for Pareto plot")

    labels = label_by or ["parallel", "gpu_count"]

    with tempfile.TemporaryDirectory(prefix="foretoken_pareto_") as tmp:
        fig_dir = Path(tmp)
        stack["plot_fig"](
            fig_dir,
            (tuple(), prepared),
            label_by=labels,
            dry_run=dry_run,
        )
        if dry_run:
            return output_path

        src = fig_dir / "PARETO.png"
        if not src.exists():
            raise FileNotFoundError(f"Pareto plot not written: {src}")
        shutil.copy(src, output_path)

    return output_path
