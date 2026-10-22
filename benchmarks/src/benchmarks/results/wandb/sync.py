# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Synchronize portable native history, then publish saved results independently of measurement."""

from __future__ import annotations

import os
import select
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

from foretoken.diagnostics import configure_logging
from loguru import logger

from benchmarks.results.recording.locks import publication_lock


def start_sync(directory: Path, native: Path) -> subprocess.Popen[bytes]:
    """Start synchronization; the recorder closes stdin after sealing its local W&B run."""
    environment = os.environ.copy()
    # A synchronizer must own its core: the recorder's offline service may stop
    # before uploads finish. Do not change the recorder's process environment.
    environment.pop("WANDB_SERVICE", None)
    environment["WANDB_MODE"] = "online"
    # The synchronizer outlives its caller. Retaining the caller's pipes would
    # keep command substitution and captured-output runners waiting for EOF.
    with (directory / "wandb-sync.log").open("ab", buffering=0) as diagnostics:
        return subprocess.Popen(
            [sys.executable, "-m", "benchmarks.results.wandb.sync", str(directory), str(native)],
            stdin=subprocess.PIPE,
            stdout=diagnostics,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )


def synchronize(directory: Path, native: Path) -> int:
    """Drain native history before acquiring the result lock for deferred publication."""
    import wandb

    from benchmarks.results.wandb.publication import sync_publication

    configure_logging(full_tracebacks=True)
    retry = f"foretoken plot {shlex.quote(str(directory))} --sync_wandb"
    with publication_lock(native):
        status = _synchronize(native, retry)
    if status:
        return status
    # Recovery takes result then native locks; release native before following that order.
    try:
        sync_publication(directory, native_synced=True)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, wandb.errors.Error) as error:
        logger.error("W&B publication failed: {}. Resume with: {}", error, retry)
        return 1
    return 0


def _synchronize(directory: Path, retry: str) -> int:
    """Delegate transfer and replay to W&B, allowing bounded drain after recording stops."""
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "wandb", "beta", "sync", "--live", str(directory)],
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        logger.error("W&B sync could not start: {}. Resume with: {}", error, retry)
        return 1
    with process:
        try:
            while process.poll() is None:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if ready and not sys.stdin.buffer.read(1):
                    # Live sync can wait forever if a recorder dies before its
                    # exit record. Let finished runs drain, then retain the native
                    # records for an explicit sync rather than leaking a process.
                    try:
                        process.wait(timeout=600)
                    except subprocess.TimeoutExpired:
                        logger.warning("W&B sync is still pending. Resume with: {}", retry)
                        return 1
                    break
            if process.returncode:
                logger.error("W&B sync stopped. Resume with: {}", retry)
            return process.returncode
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(synchronize(Path(sys.argv[1]), Path(sys.argv[2])))
