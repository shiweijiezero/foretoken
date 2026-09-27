# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared subprocess lifecycle for native evaluator integrations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import signal
import subprocess


def run_logged_process(
    command: Sequence[str],
    log_path: Path,
    *,
    quiet: bool,
    cwd: str | None = None,
    environment: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    redactions: Sequence[str] = (),
) -> int:
    """Stream a native child into a log and always reap it on interruption."""
    child_environment = os.environ.copy()
    if environment:
        child_environment.update(environment)
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            list(command),
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process:
            try:
                if stdin_text is not None:
                    process.stdin.write(stdin_text)
                    process.stdin.close()
                for line in process.stdout:
                    for secret in redactions:
                        if secret:
                            line = line.replace(secret, "[redacted]")
                    log.write(line)
                    log.flush()
                    if not quiet:
                        print(line, end="", flush=True)
                return process.wait()
            except BaseException:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
