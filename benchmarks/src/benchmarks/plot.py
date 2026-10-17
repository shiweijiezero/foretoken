# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export saved benchmark results without connecting to a model service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> None:
    """Parse the standalone plotting command and report the exported figure directory."""
    parser = argparse.ArgumentParser(
        prog="foretoken plot",
        description="Export PDF, SVG, PNG, and CSV from one or more saved-result directories.",
    )
    parser.add_argument(
        "sources", type=Path, nargs="+", metavar="RESULT_DIR",
        help="Result directories or folders containing saved results; pass several to compare"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Figure directory (default: ./plots for comparisons, RESULT_DIR/plots for one input)"
    )
    parser.add_argument(
        "--columns",
        type=int,
        choices=(1, 2),
        default=1,
        help="Paper column width (default: 1)",
    )
    parser.add_argument(
        "--metric",
        action="extend",
        nargs="+",
        default=[],
        help="One or more metrics to plot",
    )
    parser.add_argument(
        "--method",
        action="extend",
        nargs="+",
        default=[],
        help="One or more named methods to include",
    )
    parser.add_argument(
        "--base", type=Path, metavar="RESULT_DIR",
        help="Saved performance result or iteration directory to use as the base",
    )
    options = parser.parse_args(argv)
    from benchmarks.results.plots.figures import (
        default_output_directory,
        render_results,
    )

    try:
        artifacts = render_results(
            *options.sources,
            output_dir=options.output_dir,
            columns=options.columns,
            metrics=tuple(options.metric),
            methods=tuple(options.method),
            base=options.base,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Exported {len(artifacts)} files to {options.output_dir or default_output_directory(*options.sources, base=options.base)}"
    )
