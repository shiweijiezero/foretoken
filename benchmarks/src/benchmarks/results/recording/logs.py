# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain nested run diagnostics without making file capture own terminal rendering."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import (
    ExitStack,
    closing,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from io import TextIOBase
from pathlib import Path
from typing import TextIO

from tqdm.contrib.logging import logging_redirect_tqdm


@contextmanager
def capture_run_logs(directory: str, *, quiet: bool) -> Iterator[None]:
    """Attach run files to standard logging and retain raw output in enclosing runs.

    Logging handlers retain severity and tracebacks. Raw framework output is
    captured separately; only the outer run replaces the Python streams.
    """
    root = logging.getLogger()
    root_level = root.level
    diagnostics = logging.getLogger("benchmarks.results.output")
    diagnostics_level = diagnostics.level
    enclosing = sys.stdout if isinstance(sys.stdout, _RunLogStream) else None
    stream = enclosing.owner if enclosing is not None else _RunLogStream(sys.stdout)
    # Non-propagating framework loggers keep their own console configuration.
    # Attach the file at their boundary rather than duplicating propagated records.
    loggers = [root] + [
        logger for logger in list(root.manager.loggerDict.values())
        if isinstance(logger, logging.Logger) and not logger.propagate
    ]
    with closing(logging.FileHandler(Path(directory) / "run.log", encoding="utf-8")) as log:
        log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        stream.logs.append(log)
        stream.quiet_scopes.append(quiet)
        for logger in loggers:
            logger.addHandler(log)
        try:
            root.setLevel(min(root_level, logging.INFO))
            diagnostics.setLevel(logging.DEBUG)
            with ExitStack() as contexts:
                if enclosing is None:
                    console_loggers = [logger for logger in loggers if any(
                        isinstance(handler, logging.StreamHandler)
                        and not isinstance(handler, logging.FileHandler)
                        and handler.stream in (sys.stdout, sys.stderr)
                        for handler in logger.handlers
                    )]
                    contexts.enter_context(logging_redirect_tqdm(loggers=console_loggers))
                handlers = {
                    handler: handler.level
                    for logger in loggers for handler in logger.handlers
                    if isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)
                }
                for handler, level in handlers.items():
                    handler.setLevel(max(level, logging.ERROR if any(stream.quiet_scopes) else logging.INFO))
                    contexts.callback(handler.setLevel, level)
                if enclosing is None:
                    contexts.enter_context(redirect_stdout(stream))
                    contexts.enter_context(redirect_stderr(_RunLogStream(sys.stderr, owner=stream)))
                yield
        finally:
            stream.flush()
            for logger in loggers:
                logger.removeHandler(log)
            stream.logs.remove(log)
            stream.quiet_scopes.pop()
            root.setLevel(root_level)
            diagnostics.setLevel(diagnostics_level)


def terminal_stream(stream: TextIO) -> TextIO:
    """Return the actual terminal for progress renderers without recording cursor updates."""
    return stream.console if isinstance(stream, _RunLogStream) else stream


def print_result_location(message: str) -> None:
    """Show a result path or link even in quiet mode and retain it in active run logs."""
    stream = sys.stderr
    if isinstance(stream, _RunLogStream):
        stream.retain(message + "\n")
    print(message, file=terminal_stream(stream), flush=True)


class _RunLogStream(TextIOBase):
    """Retain raw framework text while standard handlers own structured log records."""

    def __init__(self, console: TextIO, *, owner: _RunLogStream | None = None) -> None:
        self.console = console
        self.owner = owner or self
        if owner is None:
            self.logs: list[logging.FileHandler] = []
            self.quiet_scopes: list[bool] = []

    def retain(self, text: str) -> None:
        """Serialize raw writes with each file handler's ordinary logging writes."""
        for handler in self.owner.logs:
            handler.acquire()
            try:
                handler.stream.write(text)
                handler.flush()
            finally:
                handler.release()

    def write(self, text: str) -> int:
        """Retain raw output and forward it unless the active result scope is quiet."""
        self.retain(text)
        if not any(self.owner.quiet_scopes):
            self.console.write(text)
        return len(text)

    def flush(self) -> None:
        """Flush retained files and terminal output before result publication or cleanup."""
        for handler in self.owner.logs:
            handler.flush()
        self.console.flush()

    def fileno(self) -> int:
        """Let native diagnostic writers target the current run rather than the caller's pipe."""
        if self.owner.logs:
            return self.owner.logs[-1].stream.fileno()
        return self.console.fileno()

    def isatty(self) -> bool:
        """Keep raw framework progress escape sequences out of retained transcripts."""
        return False
