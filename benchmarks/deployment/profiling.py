# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own profiler sessions, operator port-forwards, and local trace collection."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from benchmarks.deployment.discovery import BenchmarkEndpoint
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileTarget:
    """One ready Pod in the selected ModelService's committed serving generation."""

    pod: str
    port: int
    image: str


def discover_targets(endpoint: BenchmarkEndpoint, kubectl: Kubectl) -> tuple[ProfileTarget, ...]:
    """Resolve committed pool revisions and ready model-server Pods without changing deployment state."""
    objects = kubectl.list_resources(("modelservice", "modelpool", "modelgroup", "pod"), endpoint.namespace)
    services = [obj for obj in objects if obj["kind"] == "ModelService" and obj["metadata"]["name"] in endpoint.model_services]
    if len(services) != len(endpoint.model_services):
        raise DeploymentError("selected ModelServices are unavailable for profiling")
    revisions = {
        selected["poolUID"]: selected["revision"]
        for service in services
        for selected in service.get("status", {}).get("servingPoolRevisions", [])
    }
    pools = {
        obj["metadata"]["name"]: obj["metadata"]["uid"]
        for obj in objects if obj["kind"] == "ModelPool" and obj["metadata"]["uid"] in revisions
    }
    groups = {}
    for obj in objects:
        if obj["kind"] != "ModelGroup":
            continue
        spec = obj["spec"]
        pool = spec["modelPoolRef"]
        if pools.get(pool["name"]) == pool["uid"] and revisions.get(pool["uid"]) == spec["revision"]:
            groups[obj["metadata"]["name"]] = spec["runtime"]["port"]
    targets = []
    for obj in objects:
        if obj["kind"] != "Pod" or obj["metadata"].get("deletionTimestamp"):
            continue
        group = obj["metadata"].get("labels", {}).get("inference.foretoken.io/model-group")
        if group not in groups:
            continue
        statuses = obj.get("status", {}).get("containerStatuses", [])
        if not any(status["name"] == "model-server" and status["ready"] for status in statuses):
            raise DeploymentError("a selected model-server Pod is not ready for profiling")
        container = next(container for container in obj["spec"]["containers"] if container["name"] == "model-server")
        targets.append(ProfileTarget(obj["metadata"]["name"], groups[group], container["image"]))
    if not targets:
        raise DeploymentError("no ready model-server Pods belong to the selected serving generation")
    return tuple(sorted(targets, key=lambda target: target.pod))


class PortForward:
    """Own one loopback-only kubectl listener until capture and collection finish."""

    def __init__(self, target: ProfileTarget, namespace: str):
        self.log = tempfile.TemporaryFile(mode="w+")
        try:
            self.process = subprocess.Popen(
                ["kubectl", "port-forward", f"pod/{target.pod}", f":{target.port}", "--address=127.0.0.1", "--namespace", namespace],
                stdout=self.log, stderr=self.log, text=True,
            )
        except OSError:
            self.log.close()
            raise
        self.url = ""
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.log.seek(0)
                output = self.log.read()
                if self.process.poll() is not None:
                    raise DeploymentError(f"profiler port-forward failed: {output.strip()}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                if match:
                    self.url = f"http://127.0.0.1:{match.group(1)}"
                    return
                time.sleep(0.1)
            raise DeploymentError("timed out starting the profiler port-forward")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Terminate the owned kubectl process and close its diagnostic stream."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()


class ProfileSession:
    """Keep engine-wide captures isolated and retain artifacts when local collection fails."""

    def __init__(self, endpoint: BenchmarkEndpoint, timeout: int, output_root: str):
        self.endpoint = endpoint
        self.timeout = timeout
        self.kubectl = Kubectl()
        self.session_id = str(uuid4())
        self.output_dir = Path(output_root).expanduser().resolve() / "profiles" / self.session_id
        self.targets: list[tuple[ProfileTarget, PortForward]] = []
        self.attempted: list[tuple[ProfileTarget, PortForward]] = []
        self.artifacts: list[dict[str, Any]] = []

    def __enter__(self) -> ProfileSession:
        try:
            for target in discover_targets(self.endpoint, self.kubectl):
                self.targets.append((target, PortForward(target, self.endpoint.namespace)))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        for _, forward in reversed(self.targets):
            forward.close()
        self.targets.clear()

    async def start(self, duration_seconds: int) -> None:
        """Start capture off the event loop so in-flight warm-up requests continue to run."""
        task = asyncio.create_task(asyncio.to_thread(self._start, duration_seconds))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled await does not cancel an HTTP request in a worker thread. Wait for its
            # outcome before the runner's finally block issues stop to the same session.
            await task
            raise

    def _start(self, duration_seconds: int) -> None:
        for target, forward in self.targets:
            self.attempted.append((target, forward))
            self._request(forward, "POST", "/start", json={"session_id": self.session_id, "duration_seconds": duration_seconds})
        logger.info("Torch profiling started on %d model-server Pods", len(self.targets))

    def _request(self, forward: PortForward, method: str, suffix: str, **kwargs: Any) -> httpx.Response:
        try:
            response = httpx.request(method, f"{forward.url}/v1/internal/profile{suffix}", timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            raise DeploymentError(f"profile control failed: {error}") from error

    def stop_and_collect(self) -> None:
        """Stop every attempted capture, copy its files, then remove only successfully copied data."""
        errors: list[str] = []
        for target, forward in self.attempted:
            try:
                # An ambiguous failed start still receives stop, scoped by our UUID. A 404 means
                # that start was rejected before the server reserved this session.
                response = httpx.post(f"{forward.url}/v1/internal/profile/{self.session_id}/stop", timeout=self.timeout)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                manifest = response.json()
                files = manifest["files"]
                if not files:
                    raise DeploymentError(f"pod/{target.pod} produced no Torch profile; capture remains available for recovery")
                directory = Path(manifest["directory"])
                destination = self.output_dir / target.pod
                destination.mkdir(parents=True, exist_ok=True)
                for name in files:
                    if Path(name).name != name or name in {".", ".."}:
                        raise DeploymentError("profiler returned an invalid artifact name")
                    self.kubectl.run(["cp", f"{self.endpoint.namespace}/{target.pod}:{directory / name}", str(destination / name), "--container", "model-server"])
                self.artifacts.append({"pod": target.pod, "image": target.image, "files": [str((destination / name).relative_to(self.output_dir)) for name in files], "expired": manifest["expired"]})
                self._request(forward, "DELETE", f"/{self.session_id}")
                if manifest["expired"]:
                    errors.append(f"pod/{target.pod} reached its profile deadline; the saved trace is incomplete")
            except (httpx.HTTPError, DeploymentError, OSError, ValueError, KeyError) as error:
                errors.append(f"pod/{target.pod}: {error}")
        if errors:
            raise DeploymentError("; ".join(errors))

    def save_run(self, config: dict[str, Any], metrics: dict[str, Any] | None, error: str | None) -> None:
        """Write capture conditions locally; profile measurements never enter benchmark comparisons."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {"mode": "profile", "profiler": "torch", "session_id": self.session_id, "config": config, "request_metrics": metrics, "artifacts": self.artifacts, "error": error}
        (self.output_dir / "profile-run.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("Profile capture saved under %s", self.output_dir)
