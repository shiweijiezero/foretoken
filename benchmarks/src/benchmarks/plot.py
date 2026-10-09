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
        description="Export PDF, SVG, PNG, and CSV from a saved run or sweep.",
    )
    parser.add_argument(
        "source", type=Path, metavar="RESULT_DIR", help="Saved run or sweep directory"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Figure directory (default: RESULT_DIR/plots)"
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
        action="append",
        default=[],
        help="Metric to plot; repeat to select several",
    )
    parser.add_argument(
        "--method",
        action="append",
        default=[],
        help="Method to include; repeat to select several",
    )
    options = parser.parse_args(argv)
    from benchmarks.results.plots import render_results

    try:
        artifacts = render_results(
            options.source,
            output_dir=options.output_dir,
            columns=options.columns,
            metrics=tuple(options.metric),
            methods=tuple(options.method),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Exported {len(artifacts)} files to {options.output_dir or options.source / 'plots'}"
    )
