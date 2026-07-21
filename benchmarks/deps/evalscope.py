"""Lazy EvalScope imports (optional dependency)."""

from __future__ import annotations

from typing import Any

_INSTALL = (
    "EvalScope is required. Install with: "
    "pip install 'foretoken[evalscope]' or: pip install evalscope"
)


def require_datasets():
    try:
        import evalscope.perf.plugin.datasets  # noqa: F401
        from evalscope.perf.arguments import Arguments
        from evalscope.perf.plugin import DatasetRegistry
        from evalscope.perf.plugin.datasets.base import Turn
    except ImportError as e:
        raise ImportError(_INSTALL) from e
    return Arguments, DatasetRegistry, Turn


def require_strategies():
    try:
        from evalscope.perf.arguments import Arguments
        from evalscope.perf.core.strategies import ClosedLoopStrategy, OpenLoopStrategy
        from evalscope.perf.utils.benchmark_util import BenchmarkData
    except ImportError as e:
        raise ImportError(
            "evalscope is required for --rate / --open-loop dispatch; "
            "install with: pip install evalscope"
        ) from e
    return Arguments, ClosedLoopStrategy, OpenLoopStrategy, BenchmarkData


def require_benchmark_metrics():
    try:
        from evalscope.perf.utils.benchmark_util import BenchmarkMetrics
    except ImportError as e:
        raise ImportError(
            "evalscope is required for W&B; install with `pip install evalscope`"
        ) from e
    return BenchmarkMetrics


def require_visualizer_args():
    from evalscope.constants import VisualizerType
    from evalscope.perf.arguments import Arguments

    return VisualizerType, Arguments


def require_wandb_stack() -> dict[str, Any]:
    """WandB + EvalScope visualizer helpers used by ``WandbWriter``."""
    try:
        import wandb
        from evalscope.perf.utils import log_utils
        from evalscope.perf.utils.log_utils import (
            init_visualizer,
            maybe_log_to_visualizer,
        )
        from evalscope.utils.io_utils import current_time
    except ImportError as e:
        raise ImportError(
            "wandb/evalscope required; "
            "pip install wandb evalscope or omit --wandb"
        ) from e
    return {
        "wandb": wandb,
        "log_utils": log_utils,
        "init_visualizer": init_visualizer,
        "maybe_log_to_visualizer": maybe_log_to_visualizer,
        "current_time": current_time,
    }
