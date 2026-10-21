# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export saved benchmark results without connecting to a model service."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from foretoken.diagnostics import configure_logging
from loguru import logger


def main(argv: Sequence[str] | None = None) -> None:
    """Export saved figures and optionally synchronize original W&B runs without measuring again."""
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
    parser.add_argument(
        "--sync_wandb", action="store_true",
        help="Also synchronize saved results and per-run figures to their original W&B runs; never send model requests",
    )
    options = parser.parse_args(argv)
    from benchmarks.results.plots.export import default_output_directory, render_results

    configure_logging()
    output = (options.output_dir or default_output_directory(*options.sources, base=options.base)).resolve()
    sources = []
    errors = []
    owner = None
    if options.sync_wandb:
        from benchmarks.results.recording.directories import result_sources
        from benchmarks.results.recording.locks import publication_lock
        from benchmarks.results.wandb.publication import sync_results

        selected = [*options.sources, *([options.base] if options.base is not None else [])]
        try:
            sources = result_sources(*selected)
        except (OSError, sqlite3.Error, ValueError) as error:
            raise SystemExit(str(error)) from error
    try:
        # Seal measurements before drawing and exclude background publication while files change.
        # Release every result lock before the publisher acquires its own result/native locks.
        with ExitStack() as locks:
            for source in sorted(sources):
                locks.enter_context(publication_lock(source))
            artifacts = render_results(
                *options.sources,
                output_dir=output,
                columns=options.columns,
                metrics=tuple(options.metric),
                methods=tuple(options.method),
                base=options.base,
            )
            owner = artifacts.source
        print(f"Exported {len(artifacts.files)} files to {output}")
    except (OSError, sqlite3.Error, ValueError) as error:
        if not options.sync_wandb:
            raise SystemExit(str(error)) from error
        logger.error("Figure export failed: {}; synchronizing saved results", error)
        errors.append(error)
    if options.sync_wandb:
        try:
            sync_results(sources, figures=output, figure_owner=owner)
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            errors.append(error)
    if errors:
        raise SystemExit("; ".join(str(error) for error in errors))
