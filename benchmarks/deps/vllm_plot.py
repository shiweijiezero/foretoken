"""Lazy import of vLLM Pareto plot helpers (optional dependency)."""

from __future__ import annotations

from typing import Any, Optional

_INSTALL = (
    "vLLM Pareto helpers require vllm + pandas + seaborn. "
    "Install with: pip install 'foretoken[vllm]'"
)

_available: Optional[bool] = None
_stack: Optional[dict[str, Any]] = None


def available() -> bool:
    """True when ``vllm.benchmarks.sweep.plot_pareto`` (+ pandas/seaborn) import."""
    global _available, _stack
    if _available is not None:
        return _available
    try:
        from vllm.benchmarks.sweep.plot_pareto import (
            _pareto_frontier,
            _plot_fig,
            _prepare_records,
        )

        import pandas as pd
        import seaborn as sns  # noqa: F401  used by vLLM _plot_fig

        _stack = {
            "prepare_records": _prepare_records,
            "pareto_frontier": _pareto_frontier,
            "plot_fig": _plot_fig,
            "pd": pd,
        }
        _available = True
    except ImportError:
        _stack = None
        _available = False
    return _available


def require_pareto() -> dict[str, Any]:
    """Return ``{prepare_records, pareto_frontier, plot_fig, pd}`` or raise."""
    if not available() or _stack is None:
        raise ImportError(_INSTALL)
    return _stack
