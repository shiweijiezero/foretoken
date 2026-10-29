# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serialize updates to the small user-facing RSI report without owning its content."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import portalocker
except ImportError:  # pragma: no cover - server-only RSI checkouts may be stdlib-only
    portalocker = None

if portalocker is None:
    import fcntl


@contextmanager
def report_lock(report: Path) -> Iterator[None]:
    """Hold the task-local report lock; the caller rereads and atomically replaces prose."""
    report = report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    path = report.parent / ".foretoken-report.lock"
    with path.open("a+b") as lock:
        if portalocker is not None:
            portalocker.lock(lock, portalocker.LOCK_EX)
            try:
                yield
            finally:
                portalocker.unlock(lock)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    """Run one report update command while holding the task-local lock."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --")
    with report_lock(args.report):
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
