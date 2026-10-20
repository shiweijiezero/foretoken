# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain nested command logs while preserving the caller's terminal streams."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import TextIOBase
from pathlib import Path
from typing import TextIO


def configure_logging(console_enabled: bool) -> None:
    """Configure benchmark progress while retaining only warnings from transport and font libraries."""
    logging.basicConfig(
        level=logging.INFO if console_enabled else logging.ERROR,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        # Native evaluator imports may install handlers before CLI configuration.
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("fontTools").setLevel(logging.WARNING)


@contextmanager
def capture_run_logs(directory: str, *, quiet: bool) -> Iterator[None]:
    """Retain output in command and child logs; quiet only suppresses terminal output.

    Each nested run receives its own logging records and raw output; enclosing
    logs retain the complete execution. Only the outer scope redirects streams.
    """
    root = logging.getLogger()
    root_level = root.level
    enclosing = sys.stdout if isinstance(sys.stdout, _RunLogStream) else None
    stream = enclosing.owner if enclosing is not None else _RunLogStream(sys.stdout)
    with (Path(directory) / "run.log").open("a", encoding="utf-8", buffering=1) as log:
        stream.logs.append(log)
        stream.quiet_scopes.append(quiet)
        try:
            root.setLevel(min(root_level, logging.INFO))
            stream.capture_handlers()
            if enclosing is not None:
                yield
            else:
                stderr = _RunLogStream(sys.stderr, owner=stream)
                with redirect_stdout(stream), redirect_stderr(stderr):
                    yield
        finally:
            stream.flush()
            stream.logs.remove(log)
            stream.quiet_scopes.pop()
            if enclosing is None:
                stream.restore_handlers()
            root.setLevel(root_level)


def print_result_location(message: str) -> None:
    """Show a result path or link even in quiet mode and retain it in active run logs."""
    stream = sys.stderr
    if isinstance(stream, _RunLogStream):
        stream.retain(message + "\n")
        stream = stream.console
    print(message, file=stream, flush=True)


class _RunLogStream(TextIOBase):
    """Retain command and child logs while preserving each original terminal stream."""

    def __init__(self, console: TextIO, *, owner: _RunLogStream | None = None) -> None:
        self.console = console
        self.owner = owner or self
        if owner is None:
            self.logs: list[TextIO] = []
            self.quiet_scopes: list[bool] = []
            self.handlers: dict[
                logging.StreamHandler, tuple[int, Callable[[logging.LogRecord], bool]]
            ] = {}

    def retain(self, text: str) -> None:
        """Write output once to every enclosing run without forwarding to a terminal."""
        for log in self.owner.logs:
            log.write(text)

    def write(self, text: str) -> int:
        """Retain raw output and tee it to its original stream unless quiet is active."""
        self.retain(text)
        if not any(self.owner.quiet_scopes):
            self.console.write(text)
        return len(text)

    def flush(self) -> None:
        """Flush logs and the original terminal before publication or SDK shutdown."""
        for log in self.owner.logs:
            log.flush()
        self.console.flush()

    def isatty(self) -> bool:
        """Disable terminal-only progress formatting in retained logs."""
        return False

    def capture_handlers(self) -> None:
        """Intercept console handlers once, including handlers added by nested adapters."""
        root = logging.getLogger()
        loggers = [root] + [
            item for item in list(root.manager.loggerDict.values())
            if isinstance(item, logging.Logger)
        ]
        for item in loggers:
            for handler in item.handlers:
                if (
                    not isinstance(handler, logging.StreamHandler)
                    or isinstance(handler, logging.FileHandler)
                    or handler in self.handlers
                ):
                    continue
                stream = handler.stream
                if isinstance(stream, _RunLogStream):
                    stream = stream.console
                level = handler.level

                def capture(
                    record: logging.LogRecord, *, handler=handler, stream=stream, level=level
                ) -> bool:
                    """Retain a handler record once and honor the active terminal scope."""
                    text = handler.format(record) + handler.terminator
                    self.retain(text)
                    if (
                        record.levelno >= logging.ERROR
                        or (not any(self.quiet_scopes) and record.levelno >= level)
                    ):
                        stream.write(text)
                        stream.flush()
                    return False

                self.handlers[handler] = (level, capture)
                handler.addFilter(capture)
                handler.setLevel(logging.DEBUG)

    def restore_handlers(self) -> None:
        """Restore console filters and levels when the outer command releases capture."""
        for handler, (level, capture) in self.handlers.items():
            handler.removeFilter(capture)
            handler.setLevel(level)
            stream = handler.stream
            if isinstance(stream, _RunLogStream) and stream.owner is self:
                handler.setStream(stream.console)
