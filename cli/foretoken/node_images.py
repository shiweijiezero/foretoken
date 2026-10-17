# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own pinned source images and short-lived access to a node's containerd store."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator
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


def image_manifest_digest(
    digest: str, read: Callable[[str], dict[str, Any]]
) -> str:
    """Resolve a single-image index for node and OCI-layout consumers without unwrapping artifacts."""
    descriptor = read(digest)
    if descriptor.get("mediaType") not in {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }:
        return digest
    manifests = descriptor["manifests"]
    if len(manifests) != 1:
        return digest
    child = manifests[0]
    if (
        child["mediaType"] not in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
        or child.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
    ):
        return digest
    manifest = read(child["digest"])
    if (
        manifest.get("artifactType") or manifest.get("subject")
        or manifest.get("config", {}).get("mediaType") not in {
            "application/vnd.oci.image.config.v1+json",
            "application/vnd.docker.container.image.v1+json",
        }
    ):
        return digest
    return child["digest"]


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

    def _descriptor(self, digest: str) -> dict[str, Any]:
        """Read an OCI descriptor without exporting or unpacking image layers."""
        return json.loads(self.run([*self.containerd(), "content", "get", digest], capture=True))

    def image_digest(self, image: str) -> str | None:
        """Resolve a local build dependency from containerd metadata alone."""
        digest = self.images().get(image)
        return image_manifest_digest(digest, self._descriptor) if digest is not None else None

    def image_user(self, digest: str, architecture: str) -> str:
        """Read the build node's image USER from manifests and config, without unpacking layers."""
        manifest = self._descriptor(digest)
        while "manifests" in manifest:
            selected = next((
                entry for entry in manifest["manifests"]
                if entry.get("platform", {}).get("os") == "linux"
                and entry["platform"].get("architecture") == architecture
            ), None)
            if selected is None:
                raise DeploymentError(f"image {digest} has no linux/{architecture} manifest")
            manifest = self._descriptor(selected["digest"])
        configuration = self._descriptor(manifest["config"]["digest"])
        return configuration["config"].get("User", "") or "0"

    def matches(self, image: str, digest: str) -> bool:
        """Compare BuildKit output with a node reference, including its single-image index."""
        node_digest = self.images().get(image)
        if node_digest is None:
            return False
        if node_digest == digest:
            return True
        return image_manifest_digest(node_digest, self._descriptor) == digest

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

    def _major_version(self) -> int:
        """Select the supported containerd archive API from the node's client version."""
        version = self.run([_CONTAINERD_CLIENT, "--version"], capture=True)
        match = re.search(r"\bv([12])\.\d+\.\d+", version)
        if match is None:
            raise DeploymentError(
                f"unsupported containerd client version: {version.strip()}"
            )
        return int(match[1])

    def export_command(self, image: str, archive: str, platform: str) -> list[str]:
        """Return a synchronous OCI export for the builder's cancellation-aware executor."""
        command = [*self.containerd(), "images", "export"]
        if self._major_version() == 2:
            # The transfer client's asynchronous receiver can close an unfinished archive.
            command.append("--local")
        return [*command, "--skip-manifest-json", "--platform", platform, archive, image]

    def import_layout(self, layout: str, image: str) -> None:
        """Import and pin every OCI alias; ctr 1.x pins before its long unpack stage."""
        command = [*self.containerd(), "images", "import"]
        major = self._major_version()
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
