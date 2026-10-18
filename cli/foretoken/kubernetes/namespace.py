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
from typing import TYPE_CHECKING

from foretoken.command_execution import cleanup_commands
from foretoken.errors import DeploymentError

if TYPE_CHECKING:
    from foretoken.kubernetes.client import Kubectl

# Mutating subprocesses retain the local lock if their parent CLI exits first.
mutation_lock_fds: ContextVar[tuple[int, ...]] = ContextVar(
    "mutation_lock_fds", default=()
)


class NamespaceMutation:
    """Coordinate namespace writes across deploy, stop and temporary retirement."""

    def __init__(self, kubectl: Kubectl, namespace: str, timeout: str) -> None:
        from foretoken.kubernetes.client import timeout_seconds

        self.kubectl = kubectl
        self.namespace = namespace
        self.timeout = timeout_seconds(timeout)
        self._depth = 0
        self.directory = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "foretoken/deployment-operations"
        )

    def _recover_local(self, name: str, namespace: str) -> None:
        """Release an abandoned local record only after its CLI and inherited subprocess locks exit."""
        import fcntl

        current = self.kubectl.get_if_exists("configmap", name, namespace)
        if current is None:
            return
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
            self.kubectl.delete_object(current, f"{self.timeout:g}s")
            path.unlink(missing_ok=True)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Serialize mutations without expiring a holder that may still be executing Helm."""
        import fcntl

        from foretoken.platform.config import default_platform_config

        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        platform_namespace = default_platform_config().namespace
        name = f"foretoken-deployment-operation-{self.namespace}"
        operation = uuid.uuid4().hex
        document = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": platform_namespace},
            "data": {"operation": operation, "namespace": self.namespace},
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / operation
        with path.open("x+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            token = mutation_lock_fds.set((*mutation_lock_fds.get(), stream.fileno()))
            acquired = False
            uncertain_create = False
            try:
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeploymentError(
                            f"Timed out waiting for another deployment operation in {self.namespace}"
                        )
                    try:
                        uncertain_create = True
                        result = self.kubectl.run(
                            ["create", "--filename=-", "--output=json"],
                            input_text=json.dumps(document),
                            timeout=remaining,
                        )
                        receipt = json.loads(result.stdout)
                        acquired = True
                        break
                    except DeploymentError as error:
                        if "(AlreadyExists)" not in str(error):
                            raise
                        uncertain_create = False
                        self._recover_local(name, platform_namespace)
                        time.sleep(min(0.2, remaining))
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    with cleanup_commands(timeout=self.timeout):
                        self.kubectl.delete_object(receipt, f"{self.timeout:g}s")
                        path.unlink()
            finally:
                mutation_lock_fds.reset(token)
                # An unresolved create may still reach the API after this process exits.
                if not acquired and not uncertain_create:
                    path.unlink(missing_ok=True)
