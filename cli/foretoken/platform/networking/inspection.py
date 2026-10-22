# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Inspect node networking through short-lived Pods with read-only access."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution, cleanup_commands


class NodeInspection:
    """Own temporary network-inspection Pods using the platform's Python image and pull credentials."""

    def __init__(
        self,
        kubectl: Kubectl,
        namespace: str,
        image: str,
        pull_secrets: list[dict[str, str]],
    ) -> None:
        self._kubectl = kubectl
        self._namespace = namespace
        self._image = image
        self._pull_secrets = pull_secrets

    def run(
        self,
        node: str,
        script: str,
        timeout: str,
        *,
        arguments: tuple[str, ...] = (),
        host_paths: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run a metadata query and return its JSON result; node files are mounted read-only when needed."""
        name = "foretoken-network-inspection-" + uuid.uuid4().hex[:12]
        volumes = []
        mounts = []
        for index, (source, destination) in enumerate((host_paths or {}).items()):
            volume = f"node-files-{index}"
            volumes.append({"name": volume, "hostPath": {"path": source}})
            mounts.append({"name": volume, "mountPath": destination, "readOnly": True})
        # Installed CNI files are root-owned. Metadata-only queries need no root access.
        uid = 0 if volumes else 65532
        probe = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {"foretoken.io/network-inspection": name},
            },
            "spec": {
                "nodeName": node,
                "hostNetwork": True,
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "imagePullSecrets": self._pull_secrets,
                "tolerations": [{"operator": "Exists"}],
                "volumes": volumes,
                "containers": [
                    {
                        "name": "inspect",
                        "image": self._image,
                        "command": ["python", "-I", "-S", "-c", script, *arguments],
                        "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                        "volumeMounts": mounts,
                        "securityContext": {
                            "runAsUser": uid,
                            "runAsGroup": uid,
                            "runAsNonRoot": uid != 0,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }
        created = None
        try:
            created = json.loads(
                self._kubectl.run(
                    ["create", "-f", "-", "-o", "json"], input_text=json.dumps(probe)
                ).stdout
            )
            deadline = time.monotonic() + timeout_seconds(timeout)
            with self._kubectl.until(deadline):
                while True:
                    state = self._kubectl.get("pod", name, self._namespace)
                    status = state.get("status", {})
                    if status.get("phase") == "Failed":
                        details = [
                            status.get("reason", "Failed"),
                            status.get("message", ""),
                        ]
                        containers = status.get("containerStatuses", [])
                        for container in containers:
                            details.append(
                                f"{container['name']}: {json.dumps(container.get('state', {}))}"
                            )
                        if any(
                            container.get("containerID") for container in containers
                        ):
                            logs = self._kubectl.run(
                                ["logs", name, "--namespace", self._namespace],
                                check=False,
                            )
                            details.append(logs.stdout.strip() or logs.stderr.strip())
                        raise DeploymentError(
                            f"Network inspection on {node} failed: "
                            + "\n".join(filter(None, details))
                        )
                    if status.get("phase") == "Succeeded":
                        output = self._kubectl.run(
                            ["logs", name, "--namespace", self._namespace]
                        ).stdout
                        return json.loads(output)
                    time.sleep(0.5)
        except BaseException as error:
            # Publish the first failure before Pod retirement can block on the API.
            if execution := CommandExecution.current():
                execution.cancel(error)
            raise
        finally:
            with cleanup_commands():
                current = created or self._kubectl.get_if_exists(
                    "pod", name, self._namespace
                )
                if (
                    current is not None
                    and current["metadata"]
                    .get("labels", {})
                    .get("foretoken.io/network-inspection")
                    == name
                ):
                    self._kubectl.delete_object(current, timeout, match_version=False)
