# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep one generated workload and its retained ProfileRun in the same command lifecycle."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import CancelledError
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Self

from foretoken.arguments import ProfileCommand
from foretoken.errors import DeploymentError, current_error_reporter
from foretoken.processes.execution import CommandExecution
from foretoken.profiling import ProfileRun
from loguru import logger

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.model_service import ModelService
from benchmarks.profiling import CaptureCleanupError
from benchmarks.results.recording.files import write_json


def create_benchmark_profile(
    benchmark: BenchmarkConfig, service: ModelService, directory: str,
) -> BenchmarkProfile | None:
    """Construct the optional capture context; the HTTP runner owns its measured request window."""
    options = benchmark.profile
    if options is None:
        return None
    command = ProfileCommand(
        kustomize_path=benchmark.service.kustomize_path,
        model=service.model,
        profile_engine=options.engine,
        profile_duration=options.duration,
        timeout=benchmark.service.wait_timeout,
    )
    return BenchmarkProfile(
        ProfileRun(command, deployment=service.deployment, kubectl=service.kubectl), directory,
    )


class BenchmarkProfile:
    """Gate measured requests on capture readiness and stop after workload drain.

    The surrounding workload runner owns HTTP tasks and drains its executor before
    this context exits; the controller and runtime own recording deadlines and
    retained capture artifacts.
    """

    def __init__(self, run: ProfileRun, output_dir: str) -> None:
        self.run = run
        self.output_dir = output_dir
        self._ready: asyncio.Task[None] | None = None
        self._started = False
        self.capturing_observed_at: str | None = None
        self.first_request_at: str | None = None
        self.last_response_at: str | None = None
        self.successful_requests = 0
        self.failed_requests = 0

    def __enter__(self) -> Self:
        """Keep dataset preparation outside the capture window; start at first dispatch."""
        return self

    async def start(self) -> None:
        """Prepare capture at the first scheduled dispatch, outside the measurement clock."""
        if not self._started:
            if self._ready is None:
                self._ready = asyncio.create_task(self._start())
            await self._ready

    async def watch(self) -> None:
        """Observe an active capture for the workload task group without starting it ahead of dispatch."""
        try:
            while True:
                if self._started:
                    status = await self._observe()
                    if status.get("reason") or (
                        self.run.terminal and status["phase"] != "Succeeded"
                    ):
                        message = status.get("message") or status.get("reason")
                        raise DeploymentError(
                            f"capture {self.run.namespace}/{self.run.name} {status.get('phase', 'failed')}"
                            + (f": {message}" if message else "")
                        )
                    if self.run.terminal:
                        return
                await asyncio.sleep(1)
        except Exception as error:
            reporter = current_error_reporter()
            if reporter is not None:
                reporter(error)
            else:
                logger.opt(exception=error).error("Capture observation failed")
            raise

    async def before_request(self) -> None:
        """Record the first actual dispatch after capture startup."""
        await self.start()
        if self.first_request_at is None:
            self.first_request_at = datetime.now(UTC).isoformat()

    def response_received(self, succeeded: bool) -> None:
        """Record completed request evidence without treating HTTP success as GPU trace proof."""
        self.last_response_at = datetime.now(UTC).isoformat()
        self.successful_requests += int(succeeded)
        self.failed_requests += int(not succeeded)

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> None:
        """Report failures before confirming stop/export, then retain capture evidence on every exit."""
        reporter = current_error_reporter()
        error_active = exc_value is not None
        try:
            if exc_value is not None:
                if reporter is not None:
                    reporter(exc_value)
                self._cancel_and_wait()
                return
            if self.successful_requests == 0:
                raise DeploymentError("no successful benchmark request accompanied the capture")
            if self.failed_requests:
                self._cancel_and_wait()
                return
            self.run.observe()
            self.run.request("Finish")
            self.run.wait()
        except CaptureCleanupError:
            error_active = True
            raise
        except BaseException as error:
            error_active = True
            if reporter is not None:
                reporter(error)
            self._cancel_and_wait()
            raise
        finally:
            if self.run.uid:
                try:
                    write_json(self.output_dir, "profile.json", {
                        "namespace": self.run.namespace,
                        "name": self.run.name,
                        "uid": self.run.uid,
                        "status": self.run.status,
                        "capturing_observed_at": self.capturing_observed_at,
                        "first_request_at": self.first_request_at,
                        "last_response_at": self.last_response_at,
                        "successful_requests": self.successful_requests,
                        "failed_requests": self.failed_requests,
                    })
                except BaseException:
                    if not error_active:
                        raise
                    # The active exception also decides whether serving resources must remain.
                    logger.exception("Could not retain capture metadata")

    async def _start(self) -> None:
        """Wait for all selected runtimes to record before releasing prepared HTTP requests."""
        startup = asyncio.create_task(asyncio.to_thread(self.run.start))
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            # Kubernetes creation continues in its worker thread. Resolve its
            # final identity before the context cancels and releases resources.
            await _join_thread(startup)
            raise
        deadline = time.monotonic() + self.run.wait_seconds
        while time.monotonic() < deadline:
            status = await self._observe()
            if status.get("phase") == "Capturing":
                self.capturing_observed_at = datetime.now(UTC).isoformat()
                self._started = True
                return
            if self.run.terminal or status.get("phase") == "Stopping":
                raise DeploymentError(
                    "capture ended before benchmark requests could start: "
                    f"{status.get('phase')}: {status.get('message', '')}"
                )
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        raise DeploymentError("timed out waiting for the profile to start recording")

    async def _observe(self) -> dict[str, Any]:
        """Own one status read through cancellation before another stage can use the capture client."""
        execution = CommandExecution(self.run.wait_seconds, report_failure=False)
        observation = asyncio.create_task(asyncio.to_thread(execution.run_stage, self.run.observe))
        try:
            return await asyncio.shield(observation)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(asyncio.to_thread(execution.cancel))
            try:
                await _join_thread(cleanup)
            finally:
                try:
                    await _join_thread(observation)
                except (CancelledError, DeploymentError, OSError):
                    with suppress(CancelledError):
                        execution.check()
            raise

    def _cancel_and_wait(self) -> None:
        """Keep serving resources until cancellation and artifact publication are observed."""
        if not self.run.name or self.run.terminal:
            return
        try:
            self.run.request("Cancel")
            if self.run.uid:
                self.run.wait(require_success=False)
        except (DeploymentError, OSError, KeyboardInterrupt) as error:
            raise CaptureCleanupError(
                f"could not confirm capture cleanup: {error}; inspect ProfileRun "
                f"{self.run.namespace}/{self.run.name} before deleting the deployment"
            ) from error


async def _join_thread(task: asyncio.Task[Any]) -> Any:
    """Finish an accepted capture operation before finalization, even after repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()
