# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own one native W&B recording and its independent background synchronizer."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import wandb

from benchmarks.results.recording.database import write_records
from benchmarks.results.recording.logs import print_result_location
from benchmarks.results.types import BenchmarkRun, ResultConfiguration, _WandbPublisher
from benchmarks.results.wandb.axes import define_axis_metrics
from benchmarks.results.wandb.sync import start_sync

logger = logging.getLogger(__name__)
_SYSTEM_STATS_INTERVAL_S = 1.0


class WandbSink:
    """Record one native W&B run locally while an independent process synchronizes it."""

    def __init__(
        self,
        benchmark: ResultConfiguration,
        *,
        execution_dir: str,
        run_name: str,
        group: str,
        publisher: _WandbPublisher,
        run_config: dict[str, Any] | None = None,
    ) -> None:
        """Retain one destination for the ResultOutputs lifecycle that opens and closes it."""
        self.benchmark = benchmark
        self.execution_dir = execution_dir
        self.run_name = run_name
        self.group = group
        self.publisher = publisher
        self.run_config = run_config
        self._run: Any | None = None
        self._sync: subprocess.Popen[bytes] | None = None
        self._progress_axes: set[str] = set()

    def open(self, record: dict[str, Any]) -> None:
        """Open native recording before requests, preserving W&B's configured offline/disabled modes."""
        requested_mode = wandb.setup().settings.mode
        wandb_config = self.benchmark.wandb
        run_config = self.run_config or self.benchmark.to_dict()
        init_kwargs: dict[str, Any] = {
            "project": wandb_config.project,
            "name": self.run_name,
            "reinit": "create_new",
            "mode": "disabled" if requested_mode == "disabled" else "offline",
            "config": run_config,
            "dir": self.execution_dir,
            # Automatic code artifacts would add non-portable staging references.
            "save_code": False,
            "settings": wandb.Settings(
                silent=True,
                x_stats_sampling_interval=_SYSTEM_STATS_INTERVAL_S,
            ),
        }
        if self.group:
            init_kwargs["group"] = self.group
        if wandb_config.entity:
            init_kwargs["entity"] = wandb_config.entity
        try:
            self._run = wandb.init(**init_kwargs)
        except wandb.errors.Error:
            logger.exception("W&B local recording initialization failed")
            raise
        if self._run is None:
            raise RuntimeError("W&B initialization returned no run")
        if requested_mode != "disabled":
            directory = Path(self._run.settings.sync_dir).resolve()
            from benchmarks.results.wandb.publication import publisher_name

            identity = {
                "id": self._run.id, "project": self._run.project,
                "entity": self._run.entity, "name": self.run_name, "group": self.group,
                "base_url": self._run.settings.base_url,
                "directory": Path(os.path.relpath(directory, self.execution_dir)).as_posix(),
                "publisher": publisher_name(self.publisher),
            }
            write_records(Path(self.execution_dir), {"wandb.json": identity})
            print_result_location(f"W&B local run: {directory}")
            if requested_mode not in {"offline", "dryrun"}:
                try:
                    self._sync = start_sync(Path(self.execution_dir), directory)
                except OSError as error:
                    logger.error("W&B live sync could not start: %s; local run retained at %s", error, directory)

    def log_progress(self, row: dict[str, Any], *, axis: str) -> None:
        """Bind live fields to their domain axis and queue them through the owned run."""
        if axis not in self._progress_axes:
            define_axis_metrics(self._run, axis)
            self._progress_axes.add(axis)
        self._run.log(row)

    def publish(self, run: BenchmarkRun) -> None:
        """Record the outcome; the saved publication inputs own tables, media and artifacts."""
        if self._run is None:
            raise RuntimeError("W&B sink is not open")
        self._run.summary.update({"Foretoken/measurement_exit_code": run.exit_code})

    def close(self, *, exit_code: int = 0) -> None:
        """Seal portable native history and release background publication without blocking measurement."""
        run = self._run
        self._run = None
        if run is None:
            return
        try:
            try:
                if not run.disabled:
                    from benchmarks.results.wandb.publication import (
                        record_wandb_diagnostics,
                    )

                    record_wandb_diagnostics(Path(self.execution_dir))
            finally:
                failed = sys.exc_info()[0] is not None
                try:
                    run.finish(exit_code=exit_code)
                except (OSError, wandb.errors.Error):
                    if not failed:
                        raise
                    logger.exception("W&B recorder cleanup also failed")
        except wandb.errors.Error:
            logger.exception("W&B local recording finalization failed")
            raise
        finally:
            if self._sync is not None:
                self._sync.stdin.close()
                self._sync = None


def wandb_run_timestamp() -> str:
    """Return a local timestamp for W&B run names and generated groups."""
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
