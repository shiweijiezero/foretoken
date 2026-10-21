# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serialize one deployment's mutations without locking neighboring instances."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foretoken.errors import DeploymentError
from foretoken.processes.execution import cleanup_commands

if TYPE_CHECKING:
    from foretoken.kubernetes.client import Kubectl

_SERVICE_OPERATION_ANNOTATION = "inference.foretoken.io/service-operation"
_HOLDER_ANNOTATION = "inference.foretoken.io/deployment-holder"
_DEPLOYMENT_RESOURCE = "inferencedeployments.inference.foretoken.io"

# Mutating subprocesses retain the local lock if their parent CLI exits first.
mutation_lock_fds: ContextVar[tuple[int, ...]] = ContextVar("mutation_lock_fds", default=())


class DeploymentMutation:
    """Coordinate writes through the existing InferenceDeployment and its immutable UID."""

    def __init__(self, kubectl: Kubectl, namespace: str, name: str, timeout: str, *, uid: str = "") -> None:
        from foretoken.kubernetes.client import timeout_seconds

        self.kubectl = kubectl
        self.namespace = namespace
        self.name = name
        self.uid = uid
        self.timeout = timeout_seconds(timeout)
        self._depth = 0
        self.directory = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "foretoken/deployment-operations"
        )

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Acquire the instance record without expiring a holder whose subprocesses may still write."""
        import fcntl

        from foretoken.kubernetes.client import KubernetesVersionConflictError

        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        operation = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / operation
        with path.open("x+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            token = mutation_lock_fds.set((*mutation_lock_fds.get(), stream.fileno()))
            acquired = False
            uncertain_write = False
            try:
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeploymentError(f"Timed out waiting for another operation on deployment instance {self.name!r}")
                    current = self._read(remaining)
                    try:
                        if current["metadata"].get("annotations", {}).get(_HOLDER_ANNOTATION):
                            self._recover_local(current, remaining)
                            time.sleep(min(0.2, remaining))
                            continue
                        uncertain_write = True
                        receipt = self._set_holder(current, operation, remaining)
                        acquired = True
                        break
                    except KubernetesVersionConflictError:
                        uncertain_write = False
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    with cleanup_commands(timeout=self.timeout):
                        self._release(receipt, operation)
                        path.unlink()
            finally:
                mutation_lock_fds.reset(token)
                # A timed-out patch can still acquire ownership after the caller stops waiting.
                if not acquired and not uncertain_write:
                    path.unlink(missing_ok=True)

    def supersede(self) -> None:
        """Invalidate earlier submissions under the held lock without selecting different controller code."""
        from foretoken.kubernetes.client import KubernetesVersionConflictError

        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            current = self._read(remaining)
            metadata = current["metadata"]
            try:
                self.kubectl.run([
                    "patch", _DEPLOYMENT_RESOURCE, self.name, "--namespace", self.namespace,
                    "--type=merge", "--patch", json.dumps({
                        "metadata": {
                            "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
                            "annotations": {_SERVICE_OPERATION_ANNOTATION: uuid.uuid4().hex},
                        },
                    }),
                ], timeout=remaining)
                return
            except KubernetesVersionConflictError:
                continue
        raise DeploymentError(f"Timed out superseding deployment instance {self.name!r}")

    def _read(self, timeout: float) -> dict[str, Any]:
        """Pin operation ownership to a live instance rather than a reusable name."""
        current = self.kubectl.get(_DEPLOYMENT_RESOURCE, self.name, self.namespace, timeout=timeout)
        metadata = current["metadata"]
        if metadata.get("deletionTimestamp") or self.uid and metadata["uid"] != self.uid:
            raise DeploymentError(f"DeploymentSuperseded: deployment instance {self.name!r} was replaced or deleted")
        self.uid = metadata["uid"]
        return current

    def _set_holder(self, current: dict[str, Any], operation: str, timeout: float) -> dict[str, Any]:
        """Change only the holder with the observed record's UID and resourceVersion."""
        metadata = current["metadata"]
        return json.loads(self.kubectl.run(
            [
                "patch", _DEPLOYMENT_RESOURCE, self.name, "--namespace", self.namespace,
                "--type=merge", "--output=json", "--patch",
                json.dumps({"metadata": {
                    "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
                    "annotations": {_HOLDER_ANNOTATION: operation},
                }}),
            ], timeout=timeout,
        ).stdout)

    def _recover_local(self, current: dict[str, Any], timeout: float) -> None:
        """Release a local holder only after its CLI and inherited subprocess locks have exited."""
        import fcntl

        owner = uuid.UUID(current["metadata"]["annotations"][_HOLDER_ANNOTATION]).hex
        path = self.directory / owner
        try:
            stream = path.open("r+")
        except FileNotFoundError:
            return
        with stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._set_holder(current, "", timeout)
            path.unlink(missing_ok=True)

    def _release(self, receipt: dict[str, Any], operation: str) -> None:
        """Release this holder while preserving submitted intent and any replacement instance."""
        from foretoken.kubernetes.client import KubernetesVersionConflictError

        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            current = self.kubectl.get_if_exists(_DEPLOYMENT_RESOURCE, self.name, self.namespace, timeout=remaining)
            if current is None or current["metadata"]["uid"] != receipt["metadata"]["uid"]:
                return
            if current["metadata"].get("annotations", {}).get(_HOLDER_ANNOTATION) != operation:
                raise DeploymentError(f"Deployment operation ownership changed for {self.name!r}")
            try:
                self._set_holder(current, "", remaining)
                return
            except KubernetesVersionConflictError:
                continue
        raise DeploymentError(f"Timed out releasing deployment instance {self.name!r}")
