# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own CLI subprocesses and propagate failures across parallel lifecycle stages."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Event, Lock
from typing import Any, BinaryIO, Self

from foretoken.manifest import DeploymentError

_EXECUTIONS: ContextVar[tuple[CommandExecution, ...]] = ContextVar(
    "foretoken_command_executions", default=()
)
_OBSERVATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "foretoken_observation_deadline", default=None
)


class CommandExecution(AbstractContextManager):
    """Own an operation's commands and cleanup, retaining its first failure.

    Control commands use the caller's wait budget. Compiler stages retain their
    execution lifetime while participating in the same cancellation scope.
    """

    def __init__(self, control_timeout: float | None = None) -> None:
        self.control_timeout = control_timeout
        self._cancelled = Event()
        self._error: BaseException | None = None
        self._lock = Lock()
        self._callbacks: set[Callable[[], None]] = set()

    @classmethod
    def current(cls) -> CommandExecution | None:
        """Return the lifecycle owner active in this thread or copied context."""
        executions = _EXECUTIONS.get()
        return executions[-1] if executions else None

    def __enter__(self) -> Self:
        """Make this owner available to commands in the current lifecycle stage."""
        self.check()
        _EXECUTIONS.set((*_EXECUTIONS.get(), self))
        return self

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> None:
        """Cancel sibling stages on failure before releasing their operation scope."""
        _EXECUTIONS.set(_EXECUTIONS.get()[:-1])
        if exc_value is not None:
            self.cancel(exc_value)
            if self._error is not None and isinstance(exc_value, (CancelledError, DeploymentError, OSError)):
                raise self._error
        else:
            self.check()

    def check(self) -> None:
        """Stop subsequent work after the owning operation fails or is cancelled."""
        if self._cancelled.is_set():
            if self._error is not None:
                raise self._error
            raise CancelledError("operation cancelled")

    def register(self, callback: Callable[[], None]) -> None:
        """Register live cleanup, closing resources created concurrently with cancellation."""
        with self._lock:
            if not self._cancelled.is_set():
                self._callbacks.add(callback)
                return
        self.cleanup(callback, self._error)
        self.check()

    def unregister(self, callback: Callable[[], None]) -> None:
        """Release a resource's cancellation hook after its normal lifetime ends."""
        with self._lock:
            self._callbacks.discard(callback)

    def cleanup(self, callback: Callable[[], None], error: BaseException | None = None) -> None:
        """Allow cleanup commands to finish without masking the operation's original failure."""
        with cleanup_commands(error, timeout=self.control_timeout):
            callback()

    def cancel(self, error: BaseException | None = None) -> None:
        """Stop live resources before joining workers and retain the first failure."""
        with self._lock:
            if self._cancelled.is_set():
                return
            self._error = error
            self._cancelled.set()
            callbacks = tuple(self._callbacks)
        if callbacks:
            with ThreadPoolExecutor(max_workers=len(callbacks)) as cleanup:
                futures = [cleanup.submit(self.cleanup, callback, error) for callback in callbacks]
                for future in futures:
                    future.result()

    def run_stage(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a parallel stage with the same command owner and first-failure propagation."""
        with self:
            return function(*args, **kwargs)

    def run(
        self,
        args: list[str],
        *,
        input: str | None = None,
        stdin: BinaryIO | int | None = None,
        stdout: BinaryIO | int | None = None,
        stderr: BinaryIO | int | None = None,
        text: bool = False,
        capture_output: bool = False,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[Any]:
        """Execute a noninteractive process group and reap it on timeout or cancellation."""
        self.check()
        with subprocess.Popen(
            args,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE if capture_output else stdout,
            stderr=subprocess.PIPE if capture_output else stderr,
            text=text, cwd=cwd, env=env, pass_fds=pass_fds, start_new_session=True,
        ) as process:
            stop_lock = Lock()
            stopped = False

            def stop() -> None:
                nonlocal stopped
                with stop_lock:
                    if stopped:
                        return
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        deadline = time.monotonic() + 5
                        # A shell can exit before its children. Reap the leader,
                        # but give the whole process group its shutdown grace.
                        while True:
                            process.poll()
                            os.killpg(process.pid, 0)
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                os.killpg(process.pid, signal.SIGKILL)
                                break
                            time.sleep(min(0.05, remaining))
                    except ProcessLookupError:
                        pass
                    finally:
                        process.wait()
                        stopped = True

            self.register(stop)
            try:
                try:
                    output, errors = process.communicate(input, timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    raise DeploymentError(
                        f"{shlex.join(args)} timed out after {timeout:g}s"
                    ) from error
                # Return completed writes so their owner can record a creation receipt
                # before the enclosing stage observes concurrent cancellation.
                return subprocess.CompletedProcess(args, process.returncode, output, errors)
            except BaseException as error:
                # A caller may recover an observation timeout. Escaping the owning
                # stage, rather than an individual command, cancels sibling work.
                self.cleanup(stop, error)
                raise
            finally:
                self.unregister(stop)


@contextmanager
def command_deadline(deadline: float) -> Iterator[float]:
    """Share one observation deadline across control commands and isolate concurrent stages."""
    previous = _OBSERVATION_DEADLINE.get()
    deadline = min(previous, deadline) if previous is not None else deadline
    token = _OBSERVATION_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _OBSERVATION_DEADLINE.reset(token)


@contextmanager
def cleanup_commands(error: BaseException | None = None, *, timeout: float | None = None) -> Iterator[None]:
    """Run resource-owner cleanup after cancellation without replacing the original failure."""
    error = error or sys.exception()
    parent = CommandExecution.current()
    if timeout is None and parent is not None:
        timeout = parent.control_timeout
    token = _OBSERVATION_DEADLINE.set(time.monotonic() + timeout if timeout is not None else None)
    try:
        with CommandExecution(timeout):
            yield
    except (DeploymentError, OSError) as cleanup_error:
        print(f"Cleanup failed: {cleanup_error}", file=sys.stderr, flush=True)
        if error is None:
            raise
    finally:
        _OBSERVATION_DEADLINE.reset(token)


def run_command(args: list[str], *, timeout: float | None = None, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a control command within its owning operation's wait and cancellation budget."""
    execution = CommandExecution.current() or CommandExecution()
    if execution.control_timeout is not None:
        timeout = execution.control_timeout if timeout is None else min(timeout, execution.control_timeout)
    deadline = _OBSERVATION_DEADLINE.get()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeploymentError("Observation deadline exceeded")
        timeout = remaining if timeout is None else min(timeout, remaining)
    return execution.run(args, timeout=timeout, **kwargs)
