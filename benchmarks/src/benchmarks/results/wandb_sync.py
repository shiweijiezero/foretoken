# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run W&B's native live synchronizer independently of the benchmark recorder."""

from __future__ import annotations

import os
import select
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def start_sync(directory: Path) -> subprocess.Popen[bytes]:
    """Start synchronization; the recorder closes stdin after sealing its local W&B run."""
    environment = os.environ.copy()
    # A synchronizer must own its core: the recorder's offline service may stop
    # before uploads finish. Do not change the recorder's process environment.
    environment.pop("WANDB_SERVICE", None)
    return subprocess.Popen(
        [sys.executable, "-m", "benchmarks.results.wandb_sync", str(directory)],
        stdin=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )


@contextmanager
def publication_lock(directory: Path) -> Iterator[None]:
    """Serialize original synchronization and same-run recovery so old exit records cannot arrive last."""
    with (directory / ".foretoken-publish.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def synchronize(directory: Path) -> int:
    """Own native synchronization until its recorder exits, excluding recovery of the same run."""
    with publication_lock(directory):
        return _synchronize(directory)


def _synchronize(directory: Path) -> int:
    """Delegate transfer and replay to W&B, allowing bounded drain after recording stops."""
    retry = f"wandb sync --include-synced {shlex.quote(str(directory))}"
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "wandb", "beta", "sync", "--live", str(directory)],
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        print(f"W&B sync could not start: {error}. Resume with: {retry}", file=sys.stderr, flush=True)
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
                        print(f"W&B sync is still pending. Resume with: {retry}", file=sys.stderr, flush=True)
                        return 1
                    break
            if process.returncode:
                print(f"W&B sync stopped. Resume with: {retry}", file=sys.stderr, flush=True)
            return process.returncode
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(synchronize(Path(sys.argv[1])))
