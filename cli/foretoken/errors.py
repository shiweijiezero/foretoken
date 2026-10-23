# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Expected operation failures and one command's error-reporting boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from loguru import logger

_ERROR_REPORTER: ContextVar[Callable[[BaseException], None] | None] = ContextVar(
    "foretoken_error_reporter", default=None
)


class DeploymentError(RuntimeError):
    """A Foretoken deployment cannot be applied or inspected."""


def current_error_reporter() -> Callable[[BaseException], None] | None:
    """Borrow the CLI reporter before a command owner starts parallel stages."""
    return _ERROR_REPORTER.get()


@contextmanager
def report_command_errors() -> Iterator[None]:
    """Report command failures once, including failures observed before resource cleanup finishes."""
    reported: list[BaseException] = []
    lock = Lock()

    def report(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for item in error.exceptions:
                report(item)
            return
        if not isinstance(error, Exception):
            return
        with lock:
            if any(error is previous for previous in reported):
                return
            reported.append(error)
        logger.opt(exception=error).error("Operation failed")

    def already_reported(error: BaseException | None) -> bool:
        if isinstance(error, BaseExceptionGroup):
            return all(already_reported(item) for item in error.exceptions)
        return any(error is previous for previous in reported)

    token = _ERROR_REPORTER.set(report)
    try:
        yield
    except DeploymentError as error:
        report(error)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except BaseExceptionGroup as error:
        if already_reported(error):
            raise SystemExit(1) from error
        raise
    except SystemExit as error:
        # Benchmark entrypoints retain the original exception as the exit's cause.
        if isinstance(error.code, str) and already_reported(error.__cause__):
            raise SystemExit(1) from error.__cause__
        raise
    except Exception as error:
        if already_reported(error):
            raise SystemExit(1) from error
        raise
    finally:
        _ERROR_REPORTER.reset(token)
