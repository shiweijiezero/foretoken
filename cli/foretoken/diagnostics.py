# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure command diagnostics separately from machine-readable result output."""

from __future__ import annotations

import copy
import logging
import traceback


class _ConsoleFormatter(logging.Formatter):
    """Keep exception causes visible without copying stack traces onto the terminal."""

    def format(self, record: logging.LogRecord) -> str:
        console_record = copy.copy(record)
        console_record.exc_text = None
        console_record.stack_info = None
        return super().format(console_record)

    def formatException(self, exc_info) -> str:
        """Retain the exception type and message while file handlers keep the full traceback."""
        return "".join(traceback.format_exception_only(exc_info[0], exc_info[1])).rstrip()


def configure_logging(console_enabled: bool = True, *, full_tracebacks: bool = False) -> None:
    """Configure application-owned stderr output, retaining full traces for background logs."""
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if console_enabled else logging.ERROR)
    formatter = logging.Formatter if full_tracebacks else _ConsoleFormatter
    console.setFormatter(formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[console], force=True)
    for name in ("httpx", "httpcore", "fontTools"):
        logging.getLogger(name).setLevel(logging.WARNING)
