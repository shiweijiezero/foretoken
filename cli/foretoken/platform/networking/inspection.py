# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read node network metadata through short-lived installer Pods."""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution, cleanup_commands


class NodeInspection:
    """Own temporary network queries using the platform's selected applications and images."""

    def __init__(
        self,
        kubectl: Kubectl,
        namespace: str,
        image: str,
        pull_secrets: list[dict[str, str]],
        runtime_pod: dict[str, Any],
        runtime_class_name: str = "",
        runtime_class_nodes: tuple[str, ...] = (),
    ) -> None:
        self._kubectl = kubectl
        self._namespace = namespace
        self._image = image
        self._pull_secrets = pull_secrets
        self._runtime_pod = runtime_pod
        self._runtime_class_name = runtime_class_name
        self._runtime_class_nodes = runtime_class_nodes

    def runtime_paths(self, node: dict[str, Any], timeout: str) -> dict[str, Any]:
        """Query the running CRI implementation using the selected platform application."""
        name = node["metadata"]["name"]
        runtime = node["status"]["nodeInfo"]["containerRuntimeVersion"].split("://", 1)[
            0
        ]
        configuration = json.loads(
            self._kubectl.run(
                ["get", "--raw", f"/api/v1/nodes/{quote(name, safe='')}/proxy/configz"]
            ).stdout
        )
        endpoint = urlsplit(configuration["kubeletconfig"]["containerRuntimeEndpoint"])
        if (
            endpoint.scheme != "unix"
            or endpoint.netloc
            or not PurePosixPath(endpoint.path).is_absolute()
        ):
            raise DeploymentError(
                f"Node {name} does not expose a local Unix CRI endpoint"
            )
        # Reuse the rendered application initializer and image rather than assuming
        # the environment image contains the selected manager executable.
        source = self._runtime_pod
        container = source["containers"][0]
        spec = {
            key: copy.deepcopy(source[key])
            for key in (
                "initContainers",
                "volumes",
                "imagePullSecrets",
                "securityContext",
            )
            if key in source
        }
        spec["containers"] = [
            {
                key: copy.deepcopy(container[key])
                for key in (
                    "image",
                    "imagePullPolicy",
                    "command",
                    "volumeMounts",
                    "securityContext",
                )
                if key in container
            }
        ]
        inspector = spec["containers"][0]
        inspector["name"] = "inspect"
        inspector["args"] = [
            "inspect-node-network",
            "--endpoint=/run/foretoken/cri.sock",
            f"--runtime={runtime}",
            f"--timeout={timeout_seconds(timeout)}s",
        ]
        if self._runtime_class_name and name in self._runtime_class_nodes:
            runtime_class = self._kubectl.get("runtimeclass", self._runtime_class_name)
            inspector["args"].append("--handler=" + runtime_class["handler"])
        inspector.setdefault("securityContext", {}).update(
            {
                "runAsUser": 0,
                "runAsGroup": 0,
                "runAsNonRoot": False,
            }
        )
        spec.setdefault("volumes", []).append(
            {
                "name": "runtime-socket",
                "hostPath": {"path": endpoint.path, "type": "Socket"},
            }
        )
        inspector.setdefault("volumeMounts", []).append(
            {
                "name": "runtime-socket",
                "mountPath": "/run/foretoken/cri.sock",
                "readOnly": True,
            }
        )
        return self._run(name, spec, timeout)

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
        volumes = []
        mounts = []
        for index, (source, destination) in enumerate((host_paths or {}).items()):
            volume = f"node-files-{index}"
            volumes.append({"name": volume, "hostPath": {"path": source}})
            mounts.append({"name": volume, "mountPath": destination, "readOnly": True})
        # Installed CNI files are root-owned. Metadata-only queries need no root access.
        uid = 0 if volumes else 65532
        return self._run(
            node,
            {
                "imagePullSecrets": self._pull_secrets,
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
            timeout,
        )

    def _run(self, node: str, spec: dict[str, Any], timeout: str) -> dict[str, Any]:
        """Own creation, result collection and retirement for one node inspection."""
        name = "foretoken-network-inspection-" + uuid.uuid4().hex[:12]
        spec.update(
            {
                "nodeName": node,
                "hostNetwork": True,
                "dnsPolicy": "ClusterFirstWithHostNet",
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "tolerations": [{"operator": "Exists"}],
            }
        )
        probe = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {"foretoken.io/network-inspection": name},
            },
            "spec": spec,
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
