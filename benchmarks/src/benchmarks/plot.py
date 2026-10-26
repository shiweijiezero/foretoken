# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export saved benchmark results and report progress through one offline command."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import ExitStack

from foretoken.diagnostics import configure_logging
from loguru import logger

from benchmarks.config.plot import parse_plot_arguments


def main(argv: Sequence[str] | None = None) -> None:
    """Export saved figures and optionally synchronize original W&B runs without measuring again."""
    options = parse_plot_arguments(argv)
    from benchmarks.results.plots.export import default_output_directory, render_results

    configure_logging()
    output = (options.output_dir or default_output_directory(
        *options.sources, base=options.base, progress=options.progress,
    )).resolve()
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
                metrics=tuple(selection.key for selection in options.metrics),
                methods=options.methods,
                base=options.base,
                tradeoff=options.tradeoff,
                pricing_path=options.pricing,
                min_interactivity=options.min_interactivity,
                interactivity=options.interactivity,
                gpu_count=options.gpu_count,
                progress=options.progress,
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
