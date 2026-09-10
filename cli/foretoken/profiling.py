# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit bounded captures through Kubernetes and retrieve native runtime artifacts."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)

_MODEL_GROUP_LABEL = "inference.foretoken.io/model-group"


@dataclass(frozen=True)
class ProfileWindow:
    """One short capture, with seconds owned by the command's explicit window options."""

    delay_seconds: float = 0.0
    duration_seconds: float = 5.0

    def validate(self) -> None:
        """Reject non-finite or non-positive durations before submitting to any Pod."""
        if not math.isfinite(self.delay_seconds) or self.delay_seconds < 0:
            raise ValueError("--profile-delay must be finite and >= 0")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("--profile-duration must be finite and > 0")


@dataclass(frozen=True)
class ProfileTarget:
    """One serving model-server Pod selected for a benchmark profile."""

    pod: str
    port: int


def _name(value: dict[str, Any]) -> str:
    return str((value.get("metadata") or {}).get("name") or "")


def _uid(value: dict[str, Any]) -> str:
    return str((value.get("metadata") or {}).get("uid") or "")


def _container_ready(pod: dict[str, Any]) -> bool:
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return any(
        item.get("name") == "model-server" and item.get("ready") is True
        for item in statuses
        if isinstance(item, dict)
    )


def discover_profile_targets(
    namespace: str, model_services: tuple[str, ...], kubectl: Kubectl
) -> tuple[ProfileTarget, ...]:
    """Find Pods in the selected ModelService's committed serving generation."""
    values = kubectl.list_resources(
        ("modelservice", "modelpool", "modelgroup", "pod"),
        namespace,
    )
    services = {
        _name(value): value
        for value in values
        if value.get("kind") == "ModelService" and _name(value) in model_services
    }
    if not model_services or set(services) != set(model_services):
        raise DeploymentError("the selected ModelService resources are unavailable")

    serving_revisions: dict[str, str] = {}
    for service in services.values():
        for selected in (service.get("status") or {}).get("servingPoolRevisions", []):
            pool_uid = str(selected.get("poolUID") or "")
            revision = str(selected.get("revision") or "")
            if pool_uid and revision:
                serving_revisions[pool_uid] = revision

    pool_names = {
        _name(value): _uid(value)
        for value in values
        if value.get("kind") == "ModelPool" and _uid(value) in serving_revisions
    }
    group_ports: dict[str, int] = {}
    for value in values:
        if value.get("kind") != "ModelGroup":
            continue
        spec = value.get("spec") or {}
        pool_ref = spec.get("modelPoolRef") or {}
        pool_name = str(pool_ref.get("name") or "")
        pool_uid = str(pool_ref.get("uid") or "")
        if pool_names.get(pool_name) != pool_uid:
            continue
        if str(spec.get("revision") or "") != serving_revisions.get(pool_uid):
            continue
        port = int((spec.get("runtime") or {}).get("port") or 0)
        if port:
            group_ports[_name(value)] = port

    targets: list[ProfileTarget] = []
    for value in values:
        if value.get("kind") != "Pod" or not _container_ready(value):
            continue
        group = str(
            ((value.get("metadata") or {}).get("labels") or {}).get(
                _MODEL_GROUP_LABEL, ""
            )
        )
        if group not in group_ports:
            continue
        targets.append(ProfileTarget(_name(value), group_ports[group]))

    if not targets:
        raise DeploymentError(
            "no ready model-server Pods belong to the selected serving generation"
        )
    return tuple(sorted(targets, key=lambda item: item.pod))


class ProfileIncomplete(DeploymentError):
    """Results or stop acknowledgement are missing; temporary workloads must be retained."""


# Runs briefly inside the existing model-server container, on its existing loopback port.
# It neither owns a capture timer nor exposes a listener on the workstation.
_LOCAL_CONTROL = """
import json, sys, urllib.error, urllib.request
method, url, timeout, payload = sys.argv[1:]
request = urllib.request.Request(
    url, method=method, data=payload.encode() if payload else None,
    headers={"Content-Type": "application/json"},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    response = opener.open(request, timeout=float(timeout))
except urllib.error.HTTPError as error:
    response = error
with response:
    print(json.dumps({"status": response.code, "body": json.loads(response.read())}))
"""


class ProfileSession:
    """Own submission and retrieval for selected services; the runtime owns capture timing."""

    def __init__(
        self,
        namespace: str,
        model_services: tuple[str, ...],
        kubectl: Kubectl,
        timeout: float,
        output_root: str,
        window: ProfileWindow,
    ):
        """Prepare a command-local capture ID; submit only when the runner is ready."""
        self.namespace = namespace
        self.model_services = model_services
        self.kubectl = kubectl
        self.timeout = timeout
        self.window = window
        self.id = uuid4().hex
        self.output_dir = (
            Path(output_root).expanduser().resolve() / "profiles" / self.id
        )
        self.targets: tuple[ProfileTarget, ...] = ()

    def start(self) -> None:
        """Submit once, immediately before the runner dispatches its first prepared workload."""
        if self.targets:
            return
        self.window.validate()
        self.targets = discover_profile_targets(
            self.namespace, self.model_services, self.kubectl
        )
        payload = {
            "delay_seconds": self.window.delay_seconds,
            "duration_seconds": self.window.duration_seconds,
        }
        errors = []
        with ThreadPoolExecutor(max_workers=len(self.targets)) as executor:
            futures = [
                executor.submit(self._control, target, "PUT", payload)
                for target in self.targets
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except DeploymentError as error:
                    errors.append(str(error))
        if errors:
            # The surrounding context cancels this client-generated ID on every participant,
            # including submissions whose reply was lost, without stopping another ID.
            raise DeploymentError("; ".join(errors))
        logger.info(
            "Submitted Torch window to %s model-server Pod(s)", len(self.targets)
        )

    def _control(
        self,
        target: ProfileTarget,
        method: str,
        payload: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Use Kubernetes exec for one short local request, with no port-forward."""
        result = self.kubectl.run(
            [
                f"--request-timeout={self.timeout}s",
                "exec",
                target.pod,
                "--namespace",
                self.namespace,
                "--container",
                "model-server",
                "--",
                "sh",
                "-c",
                'exec "${FORETOKEN_VLLM_PYTHON:-python}" "$@"',
                "foretoken-profile",
                "-c",
                _LOCAL_CONTROL,
                method,
                f"http://127.0.0.1:{target.port}/v1/internal/profile/{self.id}",
                str(self.timeout),
                json.dumps(payload) if payload is not None else "",
            ]
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DeploymentError(
                f"pod/{target.pod} returned invalid profiling JSON"
            ) from error
        if response["status"] not in (200, 202):
            raise DeploymentError(
                f"profile {method} failed for pod/{target.pod}: {response['body']}"
            )
        return response["body"]

    def stop_and_collect(self) -> None:
        """Cancel remaining capture, wait for export, and copy before deleting exact artifacts."""
        if not self.targets:
            return
        errors = []
        # One participant's failure must not prevent cancellation or retrieval on the others.
        with ThreadPoolExecutor(max_workers=len(self.targets)) as executor:
            futures = [
                executor.submit(self._finish_target, target) for target in self.targets
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except (DeploymentError, OSError) as error:
                    errors.append(str(error))
        if errors:
            raise ProfileIncomplete("; ".join(errors))
        logger.info("Saved capture results to %s", self.output_dir)

    def _finish_target(self, target: ProfileTarget) -> None:
        """Keep artifacts in a Pod if its stop, status, or local copy cannot be confirmed."""
        status = self._control(target, "DELETE")
        deadline = time.monotonic() + self.timeout
        while status["phase"] not in ("completed", "cancelled", "failed"):
            if time.monotonic() >= deadline:
                raise ProfileIncomplete(
                    f"pod/{target.pod}: capture {self.id} is still {status['phase']}; "
                    "stop/export is not confirmed"
                )
            time.sleep(0.2)
            status = self._control(target, "GET")
        target_dir = self.output_dir / target.pod
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "capture.json").write_text(json.dumps(status, indent=2) + "\n")
        for remote_path in status["files"]:
            self.kubectl.run(
                [
                    "cp",
                    f"{self.namespace}/{target.pod}:{remote_path}",
                    str(target_dir / Path(remote_path).name),
                    "--container",
                    "model-server",
                ]
            )
        if status["phase"] == "failed":
            raise ProfileIncomplete(
                f"pod/{target.pod}: {status['error']}; artifacts remain in the Pod"
            )
        if status["files"]:
            self.kubectl.run(
                [
                    "exec",
                    target.pod,
                    "--namespace",
                    self.namespace,
                    "--container",
                    "model-server",
                    "--",
                    "rm",
                    "--",
                    *status["files"],
                ]
            )
        else:
            logger.warning(
                "pod/%s: workload ended before capture started; no trace was recorded",
                target.pod,
            )


@contextmanager
def benchmark_profile(
    namespace: str,
    model_services: tuple[str, ...],
    kubectl: Kubectl,
    timeout: float,
    output_root: str,
    window: ProfileWindow,
) -> Iterator[ProfileSession]:
    """Yield a lazy capture; the runner starts it at dispatch, and exit retrieves its result."""
    session = ProfileSession(
        namespace, model_services, kubectl, timeout, output_root, window
    )
    try:
        yield session
    finally:
        session.stop_and_collect()
