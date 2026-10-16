# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own pinned source images and short-lived access to a node's containerd store."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError

_IMAGE_OWNER_LABEL = "inference.foretoken.io/source-image-owner"
SOURCE_BUILD_BINDING_LABEL = "inference.foretoken.io/source-build-binding"
SOURCE_BUILD_POD_LABEL = "inference.foretoken.io/source-builder"
_PIN_LABEL = "io.cri-containerd.pinned"
_CONTAINERD_CLIENT = "/usr/local/bin/ctr"
_CONTAINERD_SOCKET = "/run/foretoken-containerd.sock"


def images_container_spec(
    tools_image: str, containerd_socket: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the shared socket-only container and host volumes for builders and cleanup."""
    client = "/bin/k3s" if "/k3s/" in containerd_socket else _CONTAINERD_CLIENT
    return (
        {
            "name": "images",
            "image": tools_image,
            "command": ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"],
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 0,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {"name": "runtime-socket", "mountPath": _CONTAINERD_SOCKET},
                {
                    "name": "runtime-client",
                    "mountPath": _CONTAINERD_CLIENT,
                    "readOnly": True,
                },
            ],
        },
        [
            {
                "name": "runtime-socket",
                "hostPath": {"path": containerd_socket, "type": "Socket"},
            },
            {"name": "runtime-client", "hostPath": {"path": client, "type": "File"}},
        ],
    )


class NodeImageStore:
    """Operate on source image records through one node's images container."""

    def __init__(
        self, kubectl: Kubectl, namespace: str, pod: str, binding: str = ""
    ) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.pod = pod
        self.binding = binding

    def run(self, args: list[str], *, capture: bool = False) -> str:
        """Execute a node image operation, surfacing failures to its lifecycle caller."""
        result = subprocess.run(
            self.kubectl.command(
                [
                    "exec",
                    "-i",
                    "-n",
                    self.namespace,
                    self.pod,
                    "-c",
                    "images",
                    "--",
                    *args,
                ]
            ),
            check=False,
            text=True,
            capture_output=capture,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise DeploymentError(
                f"node image operation failed in {self.namespace}/{self.pod}: "
                f"{detail or result.returncode}"
            )
        return result.stdout if capture else ""

    def containerd(self) -> list[str]:
        """Return the node client command used by source export and image operations."""
        return [
            _CONTAINERD_CLIENT,
            "--address",
            _CONTAINERD_SOCKET,
            "--namespace",
            "k8s.io",
        ]

    def images(self) -> dict[str, str]:
        """Return reference-to-digest records for the builder's existing cache comparison."""
        output = self.run([*self.containerd(), "images", "list"], capture=True)
        return {
            columns[0]: columns[2]
            for line in output.splitlines()[1:]
            if len(columns := line.split()) >= 3
        }

    def matches(self, image: str, digest: str) -> bool:
        """Compare BuildKit output with a node reference, including its single-image index."""
        node_digest = self.images().get(image)
        if node_digest is None:
            return False
        if node_digest == digest:
            return True
        descriptor = json.loads(
            self.run([*self.containerd(), "content", "get", node_digest], capture=True)
        )
        manifests = descriptor.get("manifests", [])
        return len(manifests) == 1 and manifests[0]["digest"] == digest

    def owned_images(self) -> dict[str, str]:
        """Return owned references and bindings, scoped to this binding when specified."""
        selector = f'labels."{_IMAGE_OWNER_LABEL}"==foretoken'
        if self.binding:
            selector += (
                f',labels."{SOURCE_BUILD_BINDING_LABEL}"=={json.dumps(self.binding)}'
            )
        output = self.run(
            [*self.containerd(), "images", "list", selector], capture=True
        )
        records = {}
        for line in output.splitlines()[1:]:
            columns = line.split()
            labels = dict(
                label.split("=", 1) for label in columns[-1].split(",") if "=" in label
            )
            records[columns[0]] = labels.get(SOURCE_BUILD_BINDING_LABEL, "")
        return records

    def pin_owned(self, image: str) -> None:
        """Refresh an existing pin only when this binding already owns the reference."""
        if image in self.owned_images():
            self._pin(image)

    def pin_records(self, records: list[tuple[str, str]]) -> None:
        """Protect recorded source outputs, reading owned references once for this batch."""
        if not records:
            return
        owned = set(self.owned_images())
        for image, digest in records:
            if image in owned or not self.matches(image, digest):
                continue
            output = self.run(
                [*self.containerd(), "images", "list", f"name=={json.dumps(image)}"],
                capture=True,
            )
            for line in output.splitlines()[1:]:
                labels = dict(
                    label.split("=", 1)
                    for label in line.split()[-1].split(",")
                    if "=" in label
                )
                if labels.get(_IMAGE_OWNER_LABEL) not in {None, "foretoken"}:
                    continue
                if labels.get(SOURCE_BUILD_BINDING_LABEL) not in {None, self.binding}:
                    continue
                self._pin(image)
                owned.add(image)

    def _pin(self, image: str) -> None:
        """Claim a source output reference and pin it before consumers can be scheduled."""
        self.run(
            [
                *self.containerd(),
                "images",
                "label",
                image,
                f"{_PIN_LABEL}=pinned",
                f"{_IMAGE_OWNER_LABEL}=foretoken",
                f"{SOURCE_BUILD_BINDING_LABEL}={self.binding}",
            ]
        )

    def import_layout(self, layout: str, image: str) -> None:
        """Import and pin every OCI alias; ctr 1.x pins before its long unpack stage."""
        command = [*self.containerd(), "images", "import"]
        version = self.run([_CONTAINERD_CLIENT, "--version"], capture=True)
        match = re.search(r"\bv([12])\.\d+\.\d+", version)
        if match is None:
            raise DeploymentError(
                f"unsupported containerd client version: {version.strip()}"
            )
        major = int(match[1])
        if major == 2:
            command.append("--local")
        index = json.loads(self.run(["cat", layout + "/index.json"], capture=True))
        references = {image}
        for manifest in index["manifests"]:
            annotations = manifest.get("annotations", {})
            reference = annotations.get("io.containerd.image.name") or annotations.get(
                "org.opencontainers.image.ref.name"
            )
            if reference:
                references.add(reference)
        # A borrowed base or another build's selection is never claimed by import.
        existing = self.images().keys() & references
        foreign = existing - self.owned_images().keys()
        if foreign:
            raise DeploymentError(
                "source image references are not owned by this build: "
                + ", ".join(sorted(foreign))
            )
        if major == 2:
            for label in (
                f"{_PIN_LABEL}=pinned",
                f"{_IMAGE_OWNER_LABEL}=foretoken",
                f"{SOURCE_BUILD_BINDING_LABEL}={self.binding}",
            ):
                command += ["--label", label]
        else:
            # ctr 1.x preserves labels on target-only updates during the second import.
            # Its initial import-to-label interval is not atomic; label errors must fail.
            self._import_layout(
                layout, [*command, "--no-unpack", "--index-name", image, "-"]
            )
            for reference in sorted(references):
                self._pin(reference)
        # ctr 1.x has already stored and pinned the content graph. Sending its
        # index again triggers unpack without rewriting every blob a second time.
        self._import_layout(
            layout, [*command, "--index-name", image, "-"], metadata_only=major == 1
        )

    def _import_layout(
        self, layout: str, command: list[str], *, metadata_only: bool = False
    ) -> None:
        """Stream the OCI archive, or its index for already stored content, to the node client."""
        members = "oci-layout index.json" if metadata_only else "."
        self.run(
            [
                "bash",
                "-ec",
                f'set -o pipefail; layout=$1; shift; tar -C "$layout" -cf - {members} | "$@"',
                "import",
                layout,
                *command,
            ]
        )

    def remove_owned(self, references: set[str]) -> None:
        """Discard this build's temporary aliases without deleting unowned references."""
        unused = references & self.owned_images().keys()
        if unused:
            self.run([*self.containerd(), "images", "remove", *sorted(unused)])

    def release_except(self, retained: set[str]) -> set[str]:
        """Release unreferenced owned pins and return owned references still retained by consumers."""
        owned = self.owned_images().keys()
        for reference in sorted(owned - retained):
            self.run(
                [
                    *self.containerd(),
                    "images",
                    "label",
                    reference,
                    _PIN_LABEL + "=",
                    _IMAGE_OWNER_LABEL + "=",
                    SOURCE_BUILD_BINDING_LABEL + "=",
                ]
            )
        return owned & retained


@contextmanager
def node_image_store(
    kubectl: Kubectl,
    namespace: str,
    node: str,
    containerd_socket: str,
    tools_image: str,
    timeout: str,
    *,
    binding: str = "",
    pull_secrets: tuple[str, ...] = (),
) -> Iterator[NodeImageStore]:
    """Borrow a Ready builder or own a socket-only helper until node cleanup finishes."""
    for pod in kubectl.list_resources(
        ("pods",),
        namespace,
        label_selector=SOURCE_BUILD_POD_LABEL + "=true",
    ):
        if (
            (
                binding
                and pod["metadata"].get("labels", {}).get(SOURCE_BUILD_BINDING_LABEL)
                != binding
            )
            or pod["spec"].get("nodeName") != node
            or pod["metadata"].get("deletionTimestamp")
            or pod.get("status", {}).get("phase") != "Running"
            or not any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ):
            continue
        if not any(c["name"] == "images" for c in pod["spec"]["containers"]):
            continue
        if not any(
            volume["name"] == "runtime-socket"
            and volume.get("hostPath", {}).get("path") == containerd_socket
            for volume in pod["spec"].get("volumes", [])
        ):
            continue
        yield NodeImageStore(kubectl, namespace, pod["metadata"]["name"], binding)
        return
    container, volumes = images_container_spec(tools_image, containerd_socket)
    name = "foretoken-node-images-" + uuid.uuid4().hex[:12]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "inference.foretoken.io/source-image-helper": "true",
                SOURCE_BUILD_BINDING_LABEL: binding,
            },
        },
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "hostNetwork": True,
            "dnsPolicy": "Default",
            "automountServiceAccountToken": False,
            "imagePullSecrets": [{"name": secret} for secret in pull_secrets],
            "containers": [container],
            "volumes": volumes,
        },
    }
    kubectl.run(["create", "-f", "-"], input_text=yaml.safe_dump(pod))
    try:
        kubectl.run(
            [
                "wait",
                "pod/" + name,
                "-n",
                namespace,
                "--for=condition=Ready",
                "--timeout=" + timeout,
            ]
        )
        yield NodeImageStore(kubectl, namespace, name, binding)
    finally:
        kubectl.run(
            [
                "delete",
                "pod",
                name,
                "-n",
                namespace,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=" + timeout,
            ]
        )
