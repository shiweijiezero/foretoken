# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serialize namespace mutations and recover locally abandoned operation ownership."""

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

from foretoken.command_execution import cleanup_commands
from foretoken.errors import DeploymentError

if TYPE_CHECKING:
    from foretoken.kubernetes.client import Kubectl

_SERVICE_OPERATION_ANNOTATION = "inference.foretoken.io/service-operation"

# Mutating subprocesses retain the local lock if their parent CLI exits first.
mutation_lock_fds: ContextVar[tuple[int, ...]] = ContextVar(
    "mutation_lock_fds", default=()
)


class NamespaceMutation:
    """Coordinate service writes through one administrator-created namespace record."""

    def __init__(self, kubectl: Kubectl, namespace: str, timeout: str) -> None:
        from foretoken.kubernetes.client import timeout_seconds
        from foretoken.deployment.namespace_access import namespace_controller_name

        self.kubectl = kubectl
        self.namespace = namespace
        self.name = namespace_controller_name() + "-operation"
        self.timeout = timeout_seconds(timeout)
        self._depth = 0
        self.directory = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "foretoken/deployment-operations"
        )

    def prepare(self) -> dict[str, Any]:
        """Create the durable operation record after an administrator has prepared its namespace."""
        current = self.kubectl.get_if_exists("configmap", self.name, self.namespace)
        if current is not None:
            return current
        document = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/managed-by": "foretoken"},
            },
        }
        try:
            return json.loads(self.kubectl.run(
                ["create", "--filename=-", "--output=json"],
                input_text=json.dumps(document), timeout=self.timeout,
            ).stdout)
        except DeploymentError as error:
            if "(AlreadyExists)" not in str(error):
                raise
            return self.kubectl.get("configmap", self.name, self.namespace)

    def supersede(self) -> None:
        """Invalidate earlier submissions under the held lock before deleting services or changing their controller."""
        current = self.kubectl.get("configmap", self.name, self.namespace)
        metadata = current["metadata"]
        self.kubectl.run([
            "patch", "configmap", self.name, "--namespace", self.namespace,
            "--type=merge", "--patch", json.dumps({
                "metadata": {
                    "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
                    "annotations": {_SERVICE_OPERATION_ANNOTATION: uuid.uuid4().hex},
                },
            }),
        ], timeout=self.timeout)

    def _set_holder(self, current: dict[str, Any], operation: str, timeout: float) -> dict[str, Any]:
        """Change only the lock holder with the observed record's UID and resourceVersion."""
        metadata = current["metadata"]
        return json.loads(self.kubectl.run(
            [
                "patch", "configmap", self.name, "--namespace", self.namespace,
                "--type=merge", "--output=json", "--patch",
                json.dumps({
                    "metadata": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
                    "data": {"operation": operation},
                }),
            ],
            timeout=timeout,
        ).stdout)

    def _recover_local(self, current: dict[str, Any], timeout: float) -> None:
        """Release a local holder only after its CLI and inherited subprocess locks have exited."""
        import fcntl

        owner = uuid.UUID(current["data"]["operation"]).hex
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
        """Release this holder while preserving submitted intent and a recreated namespace's record."""
        from foretoken.kubernetes.client import KubernetesVersionConflictError

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError(f"Timed out releasing the deployment operation in {self.namespace}")
            current = self.kubectl.get_if_exists("configmap", self.name, self.namespace, timeout=remaining)
            if current is None or current["metadata"]["uid"] != receipt["metadata"]["uid"]:
                return
            if current.get("data", {}).get("operation") != operation:
                raise DeploymentError(f"Deployment operation ownership changed in {self.namespace}")
            try:
                self._set_holder(current, "", remaining)
                return
            except KubernetesVersionConflictError:
                continue

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Acquire the namespace record without expiring a holder whose subprocesses may still write."""
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
                        raise DeploymentError(
                            f"Timed out waiting for another deployment operation in {self.namespace}"
                        )
                    current = self.kubectl.get("configmap", self.name, self.namespace, timeout=remaining)
                    try:
                        if current.get("data", {}).get("operation"):
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
