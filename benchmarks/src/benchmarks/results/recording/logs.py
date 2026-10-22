# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain nested run diagnostics while Loguru owns formatting and terminal delivery."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import TextIOBase
from pathlib import Path
from threading import RLock
from typing import TextIO

from foretoken.diagnostics import (
    console_output_enabled,
    quiet_console,
    silence_failed_console,
)
from loguru import logger


class _RunLogFile:
    """Share one transcript between Loguru and raw output, deferring I/O errors until cleanup."""

    def __init__(self, path: Path) -> None:
        """Open the scope-owned transcript and share its write lock with raw output."""
        self.path = path
        self.stream = path.open("a", encoding="utf-8")
        self.failure: OSError | None = None
        self._lock = RLock()

    def write(self, text: str) -> int:
        """Keep other destinations writable after this file fails, retaining the first failure."""
        with self._lock:
            if self.failure is None:
                try:
                    self.stream.write(text)
                except OSError as error:
                    self.failure = error
        return len(text)

    def flush(self) -> None:
        """Retain a flush failure for the scope owner while other destinations keep recording."""
        with self._lock:
            if self.failure is None:
                try:
                    self.stream.flush()
                except OSError as error:
                    self.failure = error

    def close(self) -> None:
        """Release the file after its sink is removed without replacing an active run failure."""
        with self._lock:
            try:
                self.stream.close()
            except OSError as error:
                self.failure = self.failure or error


@contextmanager
def capture_run_logs(directory: str, *, quiet: bool) -> Iterator[None]:
    """Attach one result scope's sink and retain raw output in enclosing runs.

    Detach and close the file before reporting an I/O failure. An active
    benchmark exception keeps precedence over a failure recording its logs.
    """
    enclosing = sys.stdout if isinstance(sys.stdout, _RunLogStream) else None
    stream = enclosing.owner if enclosing is not None else _RunLogStream(sys.stdout)
    log = _RunLogFile(Path(directory) / "run.log")
    sink = logger.add(
        log, level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {level} [{name}] {message}",
        colorize=False, backtrace=False, diagnose=False, enqueue=False, catch=False,
    )
    stream.logs.append(log)
    primary_error = None
    try:
        with quiet_console(quiet), ExitStack() as contexts:
            if enclosing is None:
                contexts.enter_context(redirect_stdout(stream))
                contexts.enter_context(redirect_stderr(_RunLogStream(sys.stderr, owner=stream)))
            yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            stream.flush()
        finally:
            try:
                logger.remove(sink)
            finally:
                stream.logs.remove(log)
                log.close()
        if log.failure is not None:
            if primary_error is None:
                log.failure.add_note(f"Run log: {log.path}")
                raise log.failure
            primary_error.add_note(f"Run log {log.path}: {log.failure}")
            try:
                logger.opt(exception=log.failure).warning("Could not retain run diagnostics in {}", log.path)
            except OSError:
                pass  # A failed terminal must not replace the active benchmark exception.


def terminal_stream(stream: TextIO) -> TextIO:
    """Return the actual terminal for progress renderers without recording cursor updates."""
    return stream.console if isinstance(stream, _RunLogStream) else stream


def print_result_location(message: str) -> None:
    """Show a result path or link even in quiet mode and retain it in active run logs."""
    stream = sys.stderr
    if isinstance(stream, _RunLogStream):
        stream.retain(message + "\n")
    try:
        print(message, file=terminal_stream(stream), flush=True)
    except OSError:
        silence_failed_console(terminal_stream(stream))


class _RunLogStream(TextIOBase):
    """Retain raw framework text separately from Loguru records and terminal progress."""

    def __init__(self, console: TextIO, *, owner: _RunLogStream | None = None) -> None:
        self.console = console
        self.owner = owner or self
        if owner is None:
            self.logs: list[_RunLogFile] = []

    def retain(self, text: str) -> None:
        """Write to every active transcript, even when another destination has failed."""
        for log in self.owner.logs:
            log.write(text)
            log.flush()

    def write(self, text: str) -> int:
        """Retain raw output, forwarding it only while the application console is enabled."""
        self.retain(text)
        if console_output_enabled():
            try:
                self.console.write(text)
            except OSError:
                silence_failed_console(self.console)
        return len(text)

    def flush(self) -> None:
        """Flush active transcripts and their borrowed console before capture finalization."""
        for log in self.owner.logs:
            log.flush()
        try:
            self.console.flush()
        except OSError:
            silence_failed_console(self.console)

    def fileno(self) -> int:
        """Direct native diagnostics to the current run instead of the caller's output pipe."""
        if self.owner.logs:
            return self.owner.logs[-1].stream.fileno()
        return self.console.fileno()

    def isatty(self) -> bool:
        """Keep framework cursor updates out of retained transcripts."""
        return False
