# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own pinned source images and short-lived access to a node's containerd store."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger

from foretoken.build.cluster.native_client import CLIENT_BINARY
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution, cleanup_commands

_IMAGE_OWNER_LABEL = "inference.foretoken.io/source-image-owner"
SOURCE_BUILD_BINDING_LABEL = "inference.foretoken.io/source-build-binding"
SOURCE_BUILD_POD_LABEL = "inference.foretoken.io/source-builder"
SOURCE_BUILD_STORAGE_LABEL = "inference.foretoken.io/source-build-storage"
SOURCE_IMAGE_HELPER_LABEL = "inference.foretoken.io/source-image-helper"
_PIN_LABEL = "io.cri-containerd.pinned"
_CONTAINERD_CLIENT = "/usr/local/bin/ctr"
_CONTAINERD_SOCKET = "/run/foretoken-containerd.sock"
_CONTAINERD_NAMESPACE = "k8s.io"


class NodeImageStore:
    """Operate on source image records through one node's images container."""

    def __init__(
        self, kubectl: Kubectl, namespace: str, pod: str, binding: str
    ) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.pod = pod
        self.binding = binding

    def run(self, args: list[str], *, capture: bool = False, input_text: str | None = None) -> str:
        """Execute a node image operation, surfacing failures to its lifecycle caller."""
        execution = CommandExecution.current() or CommandExecution()
        result = execution.run(
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
            text=True,
            capture_output=capture,
            input=input_text,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise DeploymentError(
                f"node image operation failed in {self.namespace}/{self.pod}: "
                f"{detail or result.returncode}"
            )
        return result.stdout if capture else ""

    def containerd(self) -> list[str]:
        """Return the node client command for image records and lease cleanup."""
        return [
            _CONTAINERD_CLIENT,
            "--address",
            _CONTAINERD_SOCKET,
            "--namespace",
            _CONTAINERD_NAMESPACE,
        ]

    def client_request(self, lease: str, platform: str) -> dict[str, Any]:
        """Project this node's access and operation ownership for the packaged native client."""
        return {
            "containerd_address": _CONTAINERD_SOCKET, "containerd_namespace": _CONTAINERD_NAMESPACE,
            "lease_id": lease, "platform": platform,
            "lease_labels": {_IMAGE_OWNER_LABEL: "foretoken", SOURCE_BUILD_BINDING_LABEL: self.binding},
        }

    def inspect(
        self, image: str, lease: str, platform: str, *, descriptor: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Read and retain a named input or generated descriptor without transferring image layers."""
        request = {**self.client_request(lease, platform), "image": image}
        if descriptor is not None:
            request["descriptor"] = descriptor
        result = json.loads(self.run([CLIENT_BINARY, "inspect"], capture=True, input_text=json.dumps(request)))
        return result if result["found"] else None

    def deliver(self, descriptor: dict[str, Any], image: str, lease: str, platform: str) -> None:
        """Publish and unpack an owned image only after its build's dependency stages have succeeded."""
        started = time.monotonic()
        logger.info("Delivering source image {} to the node", image)
        request = self.client_request(lease, platform)
        request.update(
            descriptor=descriptor, image=image,
            image_labels={**request["lease_labels"], _PIN_LABEL: "pinned"},
        )
        self.run([CLIENT_BINARY, "deliver"], input_text=json.dumps(request))
        logger.info("Source image {} delivered to the node in {:.1f}s", image, time.monotonic() - started)

    def release_leases(self, identifiers: set[str] | None = None) -> None:
        """Release this binding's native operation leases after their writer Pods have stopped."""
        output = self.run([*self.containerd(), "leases", "list"], capture=True)
        selected = []
        for line in output.splitlines()[1:]:
            columns = line.split()
            labels = dict(item.split("=", 1) for item in columns[-1].split(",") if "=" in item)
            if (
                labels.get(_IMAGE_OWNER_LABEL) == "foretoken"
                and labels.get(SOURCE_BUILD_BINDING_LABEL) == self.binding
                and (identifiers is None or columns[0] in identifiers)
            ):
                selected.append(columns[0])
        if selected:
            self.run([*self.containerd(), "leases", "delete", *selected])

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

    def matches(self, image: str, digest: str) -> bool:
        """Compare BuildKit output with a node reference, including its single-image index."""
        node_digest = self.images().get(image)
        if node_digest is None:
            return False
        if node_digest == digest:
            return True
        return image_manifest_digest(node_digest, self._descriptor) == digest

    def owned_images(self) -> dict[str, str]:
        """Return only the references whose producer is this binding."""
        selector = (
            f'labels."{_IMAGE_OWNER_LABEL}"==foretoken,'
            f'labels."{SOURCE_BUILD_BINDING_LABEL}"=={json.dumps(self.binding)}'
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
    binding: str,
    pull_secrets: tuple[str, ...] = (),
) -> Iterator[NodeImageStore]:
    """Borrow a Ready builder or own a socket-only helper until node cleanup finishes."""
    for pod in kubectl.list_resources(
        ("pods",),
        namespace,
        label_selector=SOURCE_BUILD_POD_LABEL + "=true",
    ):
        if (
            pod["metadata"].get("labels", {}).get(SOURCE_BUILD_BINDING_LABEL) != binding
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
    storage = kubectl.list_resources(
        ("configmaps",), namespace, label_selector=f"{SOURCE_BUILD_STORAGE_LABEL}={namespace}",
    )
    if len(storage) != 1:
        raise DeploymentError("source build storage metadata is unavailable")
    owner = storage[0]["metadata"]
    container, volumes = images_container_spec(tools_image, containerd_socket)
    name = "foretoken-node-images-" + uuid.uuid4().hex[:12]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                SOURCE_IMAGE_HELPER_LABEL: "true",
                SOURCE_BUILD_POD_LABEL: "true",
                SOURCE_BUILD_BINDING_LABEL: binding,
            },
            "ownerReferences": [{"apiVersion": "v1", "kind": "ConfigMap", "name": owner["name"], "uid": owner["uid"]}],
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
    metadata = None
    attempted = False
    try:
        attempted = True
        try:
            result = kubectl.run(["create", "-f", "-", "-o", "json"], input_text=json.dumps(pod))
            metadata = json.loads(result.stdout)["metadata"]
        except DeploymentError as error:
            if "AlreadyExists" in str(error):
                attempted = False
            raise
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
        with cleanup_commands(timeout=timeout_seconds(timeout)):
            current = kubectl.get_if_exists("pod", name, namespace) if attempted else None
            if current is not None and (
                metadata is not None and current["metadata"]["uid"] == metadata["uid"]
                or metadata is None and all(
                    current["metadata"].get("labels", {}).get(key) == value for key, value in pod["metadata"]["labels"].items()
                )
                and any(reference.get("uid") == owner["uid"] for reference in current["metadata"].get("ownerReferences", []))
            ):
                try:
                    kubectl.delete_object(current, timeout, match_version=False)
                except DeploymentError:
                    remaining = kubectl.get_if_exists("pod", name, namespace)
                    if remaining is not None and remaining["metadata"]["uid"] == current["metadata"]["uid"]:
                        raise


def image_manifest_digest(
    digest: str, read: Callable[[str], dict[str, Any]]
) -> str:
    """Compare a node's single-image index with build output without unwrapping artifacts."""
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


def image_tools_image(
    arguments: dict[str, str],
    registry_mirrors: dict[str, list[str]] | None = None,
) -> str:
    """Select the storage and node-client runtime through the build's Docker mirror."""
    automatic = (registry_mirrors or {}).get("docker.io", ["docker.io"])[0]
    registry = arguments.get("BASE_IMAGE_REGISTRY", automatic).rstrip("/")
    return f"{registry}/library/debian:bookworm-slim"


def node_containerd_socket(context: str, node: dict[str, Any]) -> str:
    """Resolve the node-local runtime path supported by source import and cleanup."""
    runtime = (
        node.get("status", {}).get("nodeInfo", {}).get("containerRuntimeVersion", "")
    )
    if context.startswith("k3d-") or "k3s" in runtime:
        return "/run/k3s/containerd/containerd.sock"
    if context.startswith("kind-"):
        return "/run/containerd/containerd.sock"
    return ""


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
