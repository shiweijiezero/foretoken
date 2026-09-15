# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep one generated workload and its retained ProfileRun in the same command lifecycle."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from foretoken.manifest import DeploymentError
from foretoken.profiling import ProfileRun

from benchmarks.results.output import write_json


class BenchmarkProfile:
    """Gate prepared requests on capture readiness and stop the capture after workload exit.

    EvalScope owns HTTP tasks and drains its executor before this context exits.
    The controller and runtime continue to own recording deadlines and artifacts.
    """

    def __init__(self, run: ProfileRun, output_dir: str) -> None:
        self.run = run
        self.output_dir = output_dir
        self.error: DeploymentError | None = None
        self._ready: asyncio.Task[None] | None = None
        self.capturing_observed_at: str | None = None
        self.first_request_at: str | None = None
        self.last_response_at: str | None = None
        self.successful_requests = 0

    def __enter__(self) -> BenchmarkProfile:
        """Keep dataset preparation outside the capture window; start at first dispatch."""
        return self

    async def _start(self) -> None:
        """Wait for all selected runtimes to record before releasing prepared HTTP requests."""
        await asyncio.to_thread(self.run.start)
        deadline = time.monotonic() + self.run.wait_seconds
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(self.run.observe)
            if status.get("phase") == "Capturing":
                self.capturing_observed_at = datetime.now(timezone.utc).isoformat()
                return
            if self.run.terminal or status.get("phase") == "Stopping":
                raise DeploymentError(
                    "capture ended before benchmark requests could start: "
                    f"{status.get('phase')}: {status.get('message', '')}"
                )
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        raise DeploymentError("timed out waiting for the profile to start recording")

    async def before_request(self) -> None:
        """Release each EvalScope request after a single shared startup, outside HTTP timing."""
        if self._ready is None:
            self._ready = asyncio.create_task(self._start())
        try:
            await self._ready
        except DeploymentError as error:
            self.error = error
            # EvalScope converts ordinary per-request exceptions to HTTP failures.
            # Cancellation stops the workload; its caller restores this control error.
            raise asyncio.CancelledError from error
        if self.first_request_at is None:
            self.first_request_at = datetime.now(timezone.utc).isoformat()

    def response_received(self, succeeded: bool) -> None:
        """Record completed request evidence without treating HTTP success as GPU trace proof."""
        self.last_response_at = datetime.now(timezone.utc).isoformat()
        self.successful_requests += int(succeeded)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Finish after normal workload completion, cancel on failure, and retain the run reference."""
        try:
            if exc_type is not None:
                self.run.cancel()
                return
            if self.successful_requests == 0:
                raise DeploymentError("no successful benchmark request accompanied the capture")
            self.run.observe()
            self.run.request("Finish")
            self.run.wait()
        except BaseException:
            self.run.cancel()
            raise
        finally:
            if self.run.uid:
                write_json(self.output_dir, "profile.json", {
                    "namespace": self.run.namespace,
                    "name": self.run.name,
                    "uid": self.run.uid,
                    "status": self.run.status,
                    "capturing_observed_at": self.capturing_observed_at,
                    "first_request_at": self.first_request_at,
                    "last_response_at": self.last_response_at,
                    "successful_requests": self.successful_requests,
                })
