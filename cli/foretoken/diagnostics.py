# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own application diagnostics, third-party logging and process-wide console visibility."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import TextIO

from loguru import logger
from tqdm import tqdm

_console_quiet = False


class _InterceptHandler(logging.Handler):
    """Forward SDK records to the application's Loguru sinks without changing their exceptions."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one SDK event with its source logger name and original exception."""
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.patch(lambda event: event.update(name=record.name)).opt(
            exception=record.exc_info,
        ).log(level, record.getMessage())


def _console_format(record: dict) -> str:
    """Show an exception's cause while leaving complete traceback formatting to file sinks."""
    exception = record["exception"]
    record["extra"]["console_exception"] = (
        "".join(traceback.format_exception_only(exception.type, exception.value))
        if exception else ""
    )
    return "{message}\n{extra[console_exception]}"


def _console_filter(record: dict) -> bool:
    return record["level"].no >= (logging.ERROR if _console_quiet else logging.INFO)


def configure_logging(console_enabled: bool = True, *, full_tracebacks: bool = False) -> None:
    """Configure one application's sinks before run recording starts, including native SDK loggers."""
    global _console_quiet
    _console_quiet = not console_enabled
    logger.remove()
    # Bind the original terminal before run scopes redirect raw framework output.
    logger.add(
        partial(_write_console, stream=sys.stderr),
        level="DEBUG", filter=_console_filter,
        format="{message}" if full_tracebacks else _console_format,
        colorize=False, backtrace=False, diagnose=False, enqueue=False, catch=False,
    )
    bridge = _InterceptHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    targets = [root] + [
        target for target in list(root.manager.loggerDict.values())
        if isinstance(target, logging.Logger)
    ]
    for target in targets:
        # Native framework files retain their owner. Replace only console routes,
        # and intercept non-propagating loggers at their own boundary exactly once.
        for handler in target.handlers[:]:
            if isinstance(handler, _InterceptHandler) or (
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                and handler.stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
            ):
                target.removeHandler(handler)
                handler.close()
        if target is root or not target.propagate:
            target.addHandler(bridge)
    for name in ("httpx", "httpcore", "fontTools"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _write_console(message: str, *, stream: TextIO) -> None:
    """Keep terminal I/O failures from blocking healthy file sinks and resource cleanup."""
    try:
        tqdm.write(message, file=stream, end="")
        stream.flush()
    except OSError:
        silence_failed_console(stream)


def silence_failed_console(stream: TextIO) -> None:
    """Discard a failed diagnostic stream's pending writes so final flushing preserves the exit status."""
    try:
        with open(os.devnull, "w") as discard:
            os.dup2(discard.fileno(), stream.fileno())
    except OSError:
        pass


def console_output_enabled() -> bool:
    """Return the visibility shared by terminal diagnostics and raw framework output."""
    return not _console_quiet


@contextmanager
def quiet_console(quiet: bool) -> Iterator[None]:
    """Scope console suppression with process-wide stream capture, including worker threads."""
    global _console_quiet
    previous = _console_quiet
    _console_quiet = previous or quiet
    try:
        yield
    finally:
        _console_quiet = previous
