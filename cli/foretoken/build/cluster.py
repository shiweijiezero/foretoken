# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run cached source builds beside their outputs, transferring only changed inputs."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import tarfile
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Self

import yaml

from foretoken.build.artifacts import prepare_artifact_volume
from foretoken.build.node_images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
    NodeImageStore,
    image_manifest_digest,
    images_container_spec,
)
from foretoken.build.storage import SOURCE_BUILD_UID
from foretoken.command_execution import CommandExecution, run_command
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object

_BUILD_POD_LABEL = SOURCE_BUILD_POD_LABEL
_BUILD_BINDING_LABEL = SOURCE_BUILD_BINDING_LABEL
_BUILD_MIRRORS_ANNOTATION = "inference.foretoken.io/source-build-registry-mirrors"
_BUILD_LAYOUT_ANNOTATION = "inference.foretoken.io/source-build-layout"
_BUILD_LAYOUT = "storage-tiers-v1"


class ImageNotFoundError(DeploymentError):
    """Identify a collected registry artifact that its source producer can rebuild."""


def _image_reference(image: str) -> str:
    """Normalize Docker's shorthand references for node-store lookups."""
    first = image.split("/", 1)[0]
    if "/" not in image or not ("." in first or ":" in first or first == "localhost"):
        image = "docker.io/" + ("library/" if "/" not in image else "") + image
    if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
        image += ":latest"
    return image


def _pod_ready(pod: dict[str, Any]) -> bool:
    """Return whether a source builder Pod can safely be reused."""
    return pod.get("status", {}).get("phase") == "Running" and any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def remove_build_pods(
    kubectl: Kubectl,
    timeout: str,
    *,
    binding: str = "",
    preserve_ready: bool = False,
) -> None:
    """Retire abandoned builders, or all builders during source uninstall."""
    selector = _BUILD_POD_LABEL + "=true"
    if binding:
        selector += "," + _BUILD_BINDING_LABEL + "=" + binding
    if not preserve_ready:
        kubectl.run(
            [
                "delete",
                "pods",
                "--all-namespaces",
                "--selector",
                selector,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=" + timeout,
            ]
        )
        return
    for pod in kubectl.list_all_resources(("pods",), label_selector=selector):
        # Retire obsolete cache-mounted publishers even when their compiler is healthy.
        if _pod_ready(pod) and not any(
            container["name"] == "publisher" for container in pod["spec"]["containers"]
        ):
            continue
        metadata = pod.get("metadata", {})
        name, namespace = metadata.get("name"), metadata.get("namespace")
        if name and namespace:
            kubectl.run(
                [
                    "delete",
                    "pod",
                    name,
                    "--namespace",
                    namespace,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=" + timeout,
                ]
            )


def find_build_cache(
    kubectl: Kubectl, namespace: str, binding: str, node: str, mount: str
) -> str | None:
    """Find this binding's existing compiler volume on the requested node and mount."""
    selector = f"{_BUILD_POD_LABEL}=true,{_BUILD_BINDING_LABEL}={binding}"
    for pod in kubectl.list_resources(("pods",), namespace, label_selector=selector):
        if pod["spec"].get("nodeName") != node:
            continue
        if pod["metadata"].get("annotations", {}).get(_BUILD_LAYOUT_ANNOTATION) != _BUILD_LAYOUT:
            continue
        builders = [
            container
            for container in pod["spec"]["containers"]
            if container["name"] == "builder"
        ]
        if not any(
            volume["name"] == "cache" and volume["mountPath"] == mount
            for builder in builders
            for volume in builder.get("volumeMounts", [])
        ):
            continue
        return next(
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod["spec"]["volumes"]
            if volume["name"] == "cache"
        )
    return None


def registry_credentials(images: list[str]) -> dict[str, Any]:
    """Resolve workstation login helpers only for registries used by this build."""
    path = (
        Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        / "config.json"
    )
    if not path.is_file():
        return {}
    configuration = json.loads(path.read_text())
    hosts = {image.split("/", 1)[0] for image in images if image}
    aliases = {"docker.io": "https://index.docker.io/v1/"}
    auths = {}
    for host in hosts:
        key = aliases.get(host, host)
        entries = configuration.get("auths", {})
        helper = configuration.get("credHelpers", {}).get(host)
        if not helper and (key in entries or host in entries):
            helper = configuration.get("credsStore")
        if helper:
            result = run_command(
                ["docker-credential-" + helper, "get"],
                input=key,
                text=True,
                capture_output=True,
            )
            if result.returncode:
                raise DeploymentError(f"could not read login credentials for {host}")
            credentials = json.loads(result.stdout)
            if credentials["Username"] == "<token>":
                auths[host] = {"identitytoken": credentials["Secret"]}
            else:
                token = f"{credentials['Username']}:{credentials['Secret']}".encode()
                auths[host] = {"auth": base64.b64encode(token).decode()}
        elif key in entries or host in entries:
            auths[host] = entries.get(key, entries.get(host))
    return {"auths": auths} if auths else {}


def _cache_gc_limits(quantity: str) -> tuple[str, str, str]:
    """Apply the pinned BuildKit defaults to the execution-volume budget, not the whole host disk."""
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([KMGTPE]i|[numkKMGTPE])?",
        quantity,
    )
    if match is None:
        raise DeploymentError(f"invalid compiler volume capacity: {quantity}")
    amount, suffix = match.groups()
    suffix = suffix or ""
    if suffix.endswith("i"):
        multiplier = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    else:
        multiplier = (
            Decimal(10)
            ** {
                "n": -9,
                "u": -6,
                "m": -3,
                "": 0,
                "k": 3,
                "K": 3,
                "M": 6,
                "G": 9,
                "T": 12,
                "P": 15,
                "E": 18,
            }[suffix]
        )
    capacity = int(Decimal(amount) * multiplier)
    # BuildKit 0.33 reserves 10% (up to 10 GB), keeps 20% free, and uses at most
    # 80% (up to 100 GB). Explicit image configuration remains authoritative.
    return (
        str(min(capacity // 10, 10_000_000_000)),
        str(capacity // 5),
        str(min(capacity * 4 // 5, 100_000_000_000)),
    )


class ClusterBuilder(AbstractContextManager):
    """Own one BuildKit Pod while its source workspace and build cache survive on a volume."""

    def __init__(
        self,
        kubectl: Kubectl,
        namespace: str,
        claim: str,
        mount: str,
        image: str,
        binding: str,
        timeout: str,
        *,
        tools_image: str,
        execution_claim: str = "",
        node: str = "",
        containerd_socket: str = "",
        pull_secrets: tuple[str, ...] = (),
        credentials: dict[str, Any] | None = None,
        registry_mirrors: dict[str, list[str]] | None = None,
        cancellation: CommandExecution | None = None,
    ) -> None:
        self.cancellation = cancellation or CommandExecution.current() or CommandExecution(timeout_seconds(timeout))
        self.kubectl = kubectl
        self.namespace = namespace
        self.claim = claim
        self.mount = mount.rstrip("/")
        self.image = image
        self.tools_image = tools_image
        self.binding = binding
        self.timeout = timeout
        self.node = node
        self.containerd_socket = containerd_socket
        self.pull_secrets = pull_secrets
        self.credentials = credentials or {}
        self.registry_mirrors = registry_mirrors or {}
        self.name = "foretoken-build-" + uuid.uuid4().hex[:12]
        self.root = f"{self.mount}/build/{binding}"
        self.execution_claim = execution_claim or claim
        self.execution_mount = "/var/lib/foretoken/compiler" if execution_claim and execution_claim != claim else self.mount
        self.execution_root = f"{self.execution_mount}/build/{binding}"
        self.artifact_container = "artifacts" if self.execution_claim != self.claim else "builder"
        self.runtime_root = "/run/foretoken-build"
        self.workspace = f"{self.execution_root}/workspace"
        self._image_lock = Lock()
        self._generated_layouts: dict[str, dict[str, str]] = {}
        self._used_images: set[str] = set()
        self._built_images: set[str] = set()
        self._resolved_images: dict[str, str] = {}
        self._image_users: dict[str, str] = {}

    def _reuse_or_retire_pods(self) -> dict[str, Any] | None:
        """Select a compatible daemon and retire other builders sharing its compiler cache."""
        selector = f"{_BUILD_POD_LABEL}=true,{_BUILD_BINDING_LABEL}={self.binding}"
        occupants = []
        candidates = []
        for pod in self.kubectl.list_resources(
            ("pods",), self.namespace, label_selector=selector
        ):
            if pod["metadata"].get("annotations", {}).get(_BUILD_LAYOUT_ANNOTATION) != _BUILD_LAYOUT:
                occupants.append(pod)
                continue
            if not any(
                volume["name"] == "cache"
                and volume.get("persistentVolumeClaim", {}).get("claimName")
                == self.claim
                for volume in pod["spec"].get("volumes", [])
            ):
                continue
            occupants.append(pod)
            if self.execution_claim != self.claim and not any(
                volume["name"] == "execution"
                and volume.get("persistentVolumeClaim", {}).get("claimName") == self.execution_claim
                for volume in pod["spec"].get("volumes", [])
            ):
                continue
            if self.execution_claim == self.claim and any(volume["name"] == "execution" for volume in pod["spec"].get("volumes", [])):
                continue
            if not _pod_ready(pod) or pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            if self.node and pod.get("spec", {}).get("nodeName") != self.node:
                continue
            if pod["metadata"].get("annotations", {}).get(
                _BUILD_MIRRORS_ANNOTATION, "{}"
            ) != json.dumps(self.registry_mirrors, sort_keys=True):
                continue
            containers = pod.get("spec", {}).get("containers", [])
            if self.execution_claim != self.claim and not any(
                item["name"] == "artifacts" and item["image"] == self.tools_image
                for item in containers
            ):
                continue
            builder = next(
                (
                    container
                    for container in containers
                    if container.get("name") == "builder"
                ),
                None,
            )
            if builder is None or builder.get("image") != self.image:
                continue
            if builder.get("securityContext", {}).get("runAsUser") != SOURCE_BUILD_UID:
                continue
            images = next(
                (
                    container
                    for container in containers
                    if container.get("name") == "images"
                ),
                None,
            )
            # File-only exports can reuse a daemon with an idle image-import helper.
            if self.containerd_socket and (
                images is None or images.get("image") != self.tools_image
            ):
                continue
            auth = next(
                (
                    volume.get("secret")
                    for volume in pod["spec"].get("volumes", [])
                    if volume["name"] == "registry-auth"
                ),
                None,
            )
            if bool(auth) != bool(self.credentials):
                continue
            if auth is not None:
                secret = self.kubectl.get_if_exists(
                    "secret", auth["secretName"], self.namespace
                )
                # A new Pod mounts current credentials before its first build starts.
                if secret is None or json.loads(
                    base64.b64decode(secret["data"][".dockerconfigjson"])
                ) != self.credentials:
                    continue
            candidates.append(pod)
        reusable = min(
            candidates,
            key=lambda pod: pod.get("metadata", {}).get("creationTimestamp", ""),
            default=None,
        )
        # The caller owns this binding's operation lock. Stop its previous daemon
        # before changing image or placement: the PVC still holds its root lock.
        for pod in occupants:
            if pod is reusable:
                continue
            self.kubectl.run(
                [
                    "delete",
                    "pod",
                    pod["metadata"]["name"],
                    "--namespace",
                    self.namespace,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=" + self.timeout,
                ]
            )
        return reusable

    def __enter__(self) -> Self:
        """Reuse or start the compiler daemon on the cache's node."""
        self.cancellation.check()
        self.cancellation.register(self.retire)
        try:
            reusable = self._reuse_or_retire_pods()
        except BaseException:
            self.cancellation.unregister(self.retire)
            raise
        if reusable is not None:
            self.name = reusable["metadata"]["name"]
            print(f"Reusing cluster builder {self.namespace}/{self.name}", flush=True)
            try:
                self._prepare_outputs()
            except BaseException as error:
                self.__exit__(type(error), error, error.__traceback__)
                raise
            return self
        if self.execution_claim != self.claim:
            try:
                artifact_nfs_isolated = prepare_artifact_volume(
                    self.kubectl, self.namespace, self.claim, self.binding,
                    self.node, self.tools_image, self.pull_secrets, self.timeout,
                    self.cancellation,
                )
            except BaseException:
                self.cancellation.unregister(self.retire)
                raise
        image = self.image
        pvc = self.kubectl.get("pvc", self.execution_claim, self.namespace)
        capacity = (
            pvc.get("status", {})
            .get("capacity", {})
            .get("storage", pvc["spec"]["resources"]["requests"]["storage"])
        )
        gc_limits = _cache_gc_limits(capacity)
        # Keep canonical image names so BuildKit can fall back from a mirror to the origin.
        # An image-supplied daemon configuration remains authoritative.
        mirrors = "".join(
            f"\n[registry.{json.dumps(host)}]\nmirrors = {json.dumps(endpoints)}\n"
            for host, endpoints in sorted(self.registry_mirrors.items())
        )
        spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "hostNetwork": True,
            "dnsPolicy": "Default",
            "automountServiceAccountToken": False,
            "securityContext": {"supplementalGroups": [1000]},
            "tolerations": [
                {"key": key, "operator": "Exists", "effect": "NoSchedule"}
                for key in (
                    "node-role.kubernetes.io/control-plane",
                    "node-role.kubernetes.io/master",
                )
            ],
            "imagePullSecrets": [{"name": name} for name in self.pull_secrets],
            "volumes": [
                {"name": "cache", "persistentVolumeClaim": {"claimName": self.claim}},
                {"name": "runtime", "emptyDir": {}},
            ],
            "initContainers": [
                {
                    "name": "workspace",
                    "image": image,
                    "terminationMessagePolicy": "FallbackToLogsOnError",
                    "command": [
                        "sh",
                        "-ec",
                        """persistent=$1; state=$6; uid=$7; mount=$8
filesystem=$(stat -f -c %t "$mount")
case "$filesystem" in
    6969)
        printf '%s\\n' 'The compiler volume uses NFS; use the managed local build storage or a block-backed StorageClass.' >&2
        exit 1
        ;;
esac
mkdir -p "$persistent" "$state/etc" "$state/home" "$state/runtime" "$state/tmp"
chown "$uid:1000" "$mount" "$persistent"
chmod 2775 "$mount" "$persistent"
# Snapshot ownership must stay inside the rootless UID/GID mapping. The publisher
# shares exports, not compiler snapshots. Retire legacy state after its daemon stops.
if test -d "$persistent/buildkit" && test "$(stat -c %g "$persistent/buildkit")" != "$uid"; then
    printf '%s\\n' 'Recreating compiler snapshots with private rootless ownership; published images and applications are retained.'
    rm -rf "$persistent/buildkit"
fi
mkdir -p "$persistent/buildkit"
chown "$uid:$uid" "$persistent/buildkit"
chmod 700 "$persistent/buildkit"
# Account changes are confined to this Pod, preserving the image's mapping helpers.
awk -F: -v OFS=: -v uid="$uid" -v home="$state/home" '$3 == 1000 { $3 = uid; $4 = uid; $6 = home } { print }' /etc/passwd > "$state/etc/passwd"
awk -F: -v OFS=: -v uid="$uid" '$3 == 1000 { $3 = uid } { print }' /etc/group > "$state/etc/group"
for file in subuid subgid; do
    awk -F: -v OFS=: -v uid="$uid" '$1 == 1000 { $1 = uid } { print }' "/etc/$file" > "$state/etc/$file"
done
cp -a "$HOME/." "$state/home/"
chown -R "$uid:$uid" "$state/home" "$state/runtime" "$state/tmp"
chown "$uid:$uid" "$state"
chmod 700 "$state/runtime"
chmod 644 "$state/etc/"*
configuration="${XDG_CONFIG_HOME:-$HOME/.config}/buildkit/buildkitd.toml"
if test -f "$configuration"; then
    cp "$configuration" "$state/buildkit.toml"
else
    printf "[worker.oci]\\nreservedSpace = %s\\nminFreeSpace = %s\\nmaxUsedSpace = %s\\n" \\
        "$2" "$3" "$4" > "$state/buildkit.toml"
    printf "%s" "$5" >> "$state/buildkit.toml"
fi
chown "$uid:$uid" "$state/buildkit.toml"
chmod 600 "$state/buildkit.toml"
""",
                        "prepare",
                        self.execution_root,
                        *gc_limits,
                        mirrors,
                        self.runtime_root,
                        str(SOURCE_BUILD_UID),
                        self.execution_mount,
                    ],
                    "securityContext": {"runAsUser": 0},
                    "volumeMounts": [
                        {"name": "execution" if self.execution_claim != self.claim else "cache", "mountPath": self.execution_mount},
                        {"name": "runtime", "mountPath": self.runtime_root},
                    ],
                }
            ],
        }
        container: dict[str, Any] = {
            "name": "builder",
            "image": image,
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "env": [
                {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                {"name": "HOME", "value": self.runtime_root + "/home"},
                {"name": "TMPDIR", "value": self.runtime_root + "/tmp"},
                {"name": "XDG_RUNTIME_DIR", "value": self.runtime_root + "/runtime"},
                {
                    "name": "BUILDKIT_HOST",
                    "value": "unix://" + self.runtime_root + "/runtime/buildkit/buildkitd.sock",
                },
            ],
            "workingDir": self.execution_root,
            "args": [
                "--root",
                f"{self.execution_root}/buildkit",
                "--config",
                f"{self.runtime_root}/buildkit.toml",
            ],
            "volumeMounts": [
                {"name": "cache", "mountPath": self.mount},
                {"name": "runtime", "mountPath": self.runtime_root},
                *(
                    {
                        "name": "runtime",
                        "mountPath": "/etc/" + name,
                        "subPath": "etc/" + name,
                        "readOnly": True,
                    }
                    for name in ("passwd", "group", "subuid", "subgid")
                ),
            ],
            "readinessProbe": {
                "exec": {"command": ["buildctl", "debug", "workers"]},
                "periodSeconds": 2,
            },
        }
        container["args"].append("--oci-worker-no-process-sandbox")
        container["securityContext"] = {
            "runAsUser": SOURCE_BUILD_UID,
            "runAsGroup": SOURCE_BUILD_UID,
            "seccompProfile": {"type": "Unconfined"},
            "appArmorProfile": {"type": "Unconfined"},
        }
        if self.node:
            spec["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchFields": [
                                    {
                                        "key": "metadata.name",
                                        "operator": "In",
                                        "values": [self.node],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        if self.credentials:
            spec["imagePullSecrets"].append({"name": self.name})
            spec["volumes"].append(
                {
                    "name": "registry-auth",
                    "secret": {
                        "secretName": self.name,
                        "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
                    },
                }
            )
            container["volumeMounts"].append(
                {
                    "name": "registry-auth",
                    "mountPath": "/registry-auth",
                    "readOnly": True,
                }
            )
            container["env"].append(
                {"name": "DOCKER_CONFIG", "value": "/registry-auth"}
            )
        if self.execution_claim != self.claim:
            spec["volumes"].append({
                "name": "execution",
                "persistentVolumeClaim": {"claimName": self.execution_claim},
            })
            container["volumeMounts"].append({"name": "execution", "mountPath": self.execution_mount})
            # The storage server's ordinary writer creates only this binding's
            # staging tree; root-squashed NFS does not require host chown.
            spec["initContainers"].append({
                "name": "prepare-artifacts",
                "image": self.tools_image,
                "command": [
                    "sh", "-ec",
                    """if test "$(stat -f -c %t "$2")" = 6969 && test "$3" != true; then
    printf '%s\\n' 'This NFS artifact provider has no supported isolated mount lifecycle; use a native NFS or nfs.csi.k8s.io volume.' >&2
    exit 1
fi
if test "$3" = true; then
    if ! test -d "$1"; then mkdir -p "$1"; chmod 2770 "$1"; fi
else
    mkdir -p "$1"
    chown "$4:1000" "$1" "$2"
    chmod 2775 "$1" "$2"
fi
if ! test -w "$1"; then
    printf '%s\\n' 'Artifact storage must allow writes by publisher UID 1000 and group 1000; check the provisioner directory permissions.' >&2
    exit 1
fi
""",
                    "prepare", self.root, self.mount, str(artifact_nfs_isolated).lower(), str(SOURCE_BUILD_UID),
                ],
                "terminationMessagePolicy": "FallbackToLogsOnError",
                "securityContext": {
                    "runAsUser": 1000 if artifact_nfs_isolated else 0,
                    "runAsGroup": 1000,
                    "allowPrivilegeEscalation": False,
                },
                "volumeMounts": [{"name": "cache", "mountPath": self.mount}],
            })
        spec["containers"] = [container]
        if self.execution_claim != self.claim:
            spec["containers"].append({
                "name": "artifacts",
                "image": self.tools_image,
                "command": ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"],
                "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                "securityContext": {
                    "runAsUser": 1000 if artifact_nfs_isolated else SOURCE_BUILD_UID,
                    "runAsGroup": 1000,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "cache", "mountPath": self.mount},
                    {"name": "execution", "mountPath": self.execution_mount, "readOnly": True},
                ],
            })
        if self.containerd_socket:
            # Only local kind/k3d installation uses node image import. The compiler
            # never receives the runtime socket; archives remain on the cluster volume.
            # GNU tar preserves PAX sizes for OCI layer blobs larger than 8 GiB.
            images, volumes = images_container_spec(
                self.tools_image, self.containerd_socket
            )
            images["securityContext"]["runAsUser"] = SOURCE_BUILD_UID
            images["volumeMounts"].insert(0, {"name": "cache", "mountPath": self.mount})
            spec["volumes"] += volumes
            spec["containers"].append(images)
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "annotations": {
                    _BUILD_LAYOUT_ANNOTATION: _BUILD_LAYOUT,
                    _BUILD_MIRRORS_ANNOTATION: json.dumps(
                        self.registry_mirrors, sort_keys=True
                    ),
                },
                "labels": {
                    _BUILD_POD_LABEL: "true",
                    _BUILD_BINDING_LABEL: self.binding,
                },
            },
            "spec": spec,
        }
        print(f"Preparing cluster builder {self.namespace}/{self.name}", flush=True)
        deadline = time.monotonic() + timeout_seconds(self.timeout)

        def ready(value: dict[str, Any]) -> bool:
            """Surface terminal startup failures while their Kubernetes diagnostics still exist."""
            self.cancellation.check()
            status = value.get("status", {})
            for field, initializing in (("initContainerStatuses", True), ("containerStatuses", False)):
                for container_status in status.get(field, []):
                    terminated = container_status.get("state", {}).get("terminated")
                    if terminated is None or initializing and terminated["exitCode"] == 0:
                        continue
                    detail = terminated.get("message") or terminated.get("reason") or "container exited"
                    raise DeploymentError(
                        f"cluster builder {self.namespace}/{self.name}: "
                        f"{container_status['name']} exited with {terminated['exitCode']}: {detail}"
                    )
            if status.get("phase") in {"Failed", "Succeeded"}:
                raise DeploymentError(
                    f"cluster builder {self.namespace}/{self.name} stopped before becoming ready: "
                    + (status.get("message") or status["phase"])
                )
            return _pod_ready(value)

        try:
            self.cancellation.check()
            created = self.kubectl.run(
                ["create", "-f", "-", "-o", "json"], input_text=yaml.safe_dump(pod)
            )
            self.cancellation.check()
            pod_uid = json.loads(created.stdout)["metadata"]["uid"]
            if self.credentials:
                self.kubectl.run(
                    ["create", "-f", "-"],
                    input_text=yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "type": "kubernetes.io/dockerconfigjson",
                            "metadata": {
                                "name": self.name,
                                "namespace": self.namespace,
                                "ownerReferences": [
                                    {
                                        "apiVersion": "v1",
                                        "kind": "Pod",
                                        "name": self.name,
                                        "uid": pod_uid,
                                    }
                                ],
                            },
                            "data": {
                                ".dockerconfigjson": base64.b64encode(
                                    json.dumps(self.credentials).encode()
                                ).decode()
                            },
                        }
                    ),
                )
            wait_for_object(
                lambda: self.kubectl.get(
                    "pod", self.name, self.namespace,
                    timeout=max(1.0, deadline - time.monotonic()),
                ),
                ready,
                deadline,
                f"cluster builder {self.namespace}/{self.name}",
            )
            self._prepare_outputs()
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> None:
        """Retain successful builders and clean transient or failed build state."""
        try:
            if exc_type is not None:
                # A disconnected buildctl can leave executor children alive in the
                # rootless worker's shared PID namespace. Retire reused Pods as well.
                self.cancellation.cleanup(self.retire, exc_value)
                return
            try:
                self._retain_build_cache()
            except BaseException as error:
                self.cancellation.cleanup(self.retire, error)
                raise
        finally:
            self.cancellation.unregister(self.retire)

    def _prepare_outputs(self) -> None:
        """Clear interrupted outputs under the binding lock, retaining only recorded OCI layouts."""
        layouts = self.read_json(self.root + "/images.json")
        self.run([
            "sh", "-ec", '''root=$1; shift
rm -rf -- "$root/transfers" "$root/output"
for directory in "$root/images/"*; do
    keep=
    for retained; do
        if test "$directory" = "$retained"; then keep=1; break; fi
    done
    if test -z "$keep"; then rm -rf -- "$directory"; fi
done
''',
            "prepare", self.root, *sorted({layout["path"] for layout in layouts.values()}),
        ], container=self.artifact_container)
        self.run([
            "sh", "-ec", 'rm -rf "$1/results" "$1/exports"; mkdir -p "$1/results" "$1/exports"',
            "prepare", self.execution_root,
        ])

    def _retain_build_cache(self) -> None:
        """Remove transient outputs and unreferenced OCI layouts after successful execution."""
        self.run(["rm", "-rf", "--", self.execution_root + "/results", self.execution_root + "/exports"])
        self.run(["rm", "-rf", "--", self.root + "/transfers"], container=self.artifact_container)
        if self._used_images:
            path = self.root + "/images.json"
            layouts = self.read_json(path)
            stale = [
                value["path"]
                for image, value in layouts.items()
                if image not in self._used_images
            ]
            retained = {
                image: value
                for image, value in layouts.items()
                if image in self._used_images
            }
            encoded = shlex.quote(json.dumps(retained))
            self.run(
                [
                    "sh",
                    "-ec",
                    f'printf %s {encoded} > "$1.next"; mv "$1.next" "$1"',
                    "save",
                    path,
                ]
            )
            if stale:
                self.run(["rm", "-rf", "--", *stale], container=self.artifact_container)

    def retire(self) -> None:
        """Stop this binding's daemon and executors while retaining its last saved artifacts."""
        self.kubectl.run(
            [
                "delete", "pod", self.name, "-n", self.namespace,
                "--ignore-not-found", "--wait=true", "--timeout=" + self.timeout,
            ]
        )

    def command(self, args: list[str], *, container: str = "builder") -> list[str]:
        """Address the build Pod for both streamed input and ordinary commands."""
        return self.kubectl.command(
            [
                "exec",
                "-i",
                "-n",
                self.namespace,
                self.name,
                "-c",
                container,
                "--",
                *args,
            ]
        )

    def run(
        self,
        args: list[str],
        *,
        capture: bool = False,
        container: str = "builder",
        input_text: str | None = None,
    ) -> str:
        """Execute a build stage, preserving live compiler and daemon diagnostics on failure."""
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        result = self.cancellation.run(
            self.command(args, container=container),
            text=True,
            capture_output=capture,
            input=input_text,
        )
        if result.returncode:
            self.cancellation.check()
            detail = (result.stderr or result.stdout or "").strip()
            if container == "builder" and args[0] == "buildctl":
                # Daemon diagnostics can explain an otherwise bare exit code, but
                # must not postpone a known build failure or replace its cause.
                diagnostic_timeout = min(5.0, timeout_seconds(self.timeout))
                try:
                    logs = run_command(
                        self.kubectl.command([
                            "logs", "-n", self.namespace, self.name, "-c", "builder",
                            "--since-time=" + started, "--timestamps",
                            f"--request-timeout={diagnostic_timeout:g}s",
                        ]),
                        text=True, capture_output=True, timeout=diagnostic_timeout,
                    )
                except (DeploymentError, OSError) as error:
                    print(f"Builder logs unavailable: {error}", flush=True)
                else:
                    if logs.returncode == 0 and logs.stdout.strip():
                        print(logs.stdout, flush=True)
                        if any(
                            "session key" in line and "disk quota exceeded" in line
                            for line in logs.stdout.splitlines()
                        ):
                            detail += ("\n" if detail else "") + (
                                "BuildKit reported an exhausted host keyring quota for the builder's UID; "
                                "inspect /proc/key-users and kernel.keys.maxkeys/maxbytes on the host, "
                                "then identify retained keyring references or adjust host key capacity"
                            )
                    elif logs.returncode:
                        print(f"Builder logs unavailable: {logs.stderr.strip()}", flush=True)
            raise DeploymentError(
                f"cluster build failed in {self.namespace}/{self.name}"
                + (f" on node {self.node}" if self.node else "")
                + f": {detail or result.returncode}"
            )
        return result.stdout if capture else ""

    def read_json(self, path: str, *, container: str = "builder") -> dict[str, Any]:
        """Read optional operation metadata without hiding malformed or unreadable state."""
        text = self.run(
            [
                "sh",
                "-ec",
                'if test -f "$1"; then cat "$1"; else printf "{}"; fi',
                "read",
                path,
            ],
            capture=True,
            container=container,
        )
        return json.loads(text)

    def sync(self, files: dict[str, Path], versions: dict[str, str]) -> None:
        """Apply a path-level delta; an interrupted upload is replaced completely on retry."""
        started = time.monotonic()
        marker = self.execution_root + "/inputs.json"
        previous = self.read_json(marker)
        changed = [name for name in files if previous.get(name) != versions[name]]
        removed = sorted(previous.keys() - versions.keys())
        if not changed and not removed:
            print("Source transfer: reused unchanged workspace", flush=True)
            return
        # The receipt is installed last. Without it a retry starts from an empty managed
        # workspace, rather than accepting files left by a partially received archive.
        reset = not previous
        script = """set -eu
root=$1; workspace=$2; reset=$3; incoming="$root/incoming"
rm -rf "$incoming"; mkdir -p "$incoming"
tar --no-same-owner -xzf - -C "$incoming"
rm -f "$root/inputs.json"
if test "$reset" = true; then rm -rf "$workspace"; fi
mkdir -p "$workspace"; cd "$workspace"
xargs -0 -r rm -f -- < "$incoming/.foretoken-removed"
find . -mindepth 1 -depth -type d -empty -delete
rm "$incoming/.foretoken-removed"
mv "$incoming/.foretoken-inputs" "$root/inputs.next"
# Cargo and Ninja must observe changed bytes even when client timestamps were restored.
find "$incoming" -type f -exec touch {} +
cp -pRf "$incoming/." "$workspace/"
mv "$root/inputs.next" "$root/inputs.json"
rm -rf "$incoming"
"""
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for name in changed:
                    archive.add(files[name], arcname=name, recursive=False)
                for name, data in (
                    (".foretoken-removed", b"\0".join(p.encode() for p in removed)),
                    (".foretoken-inputs", json.dumps(versions).encode()),
                ):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.mode = 0o644
                    archive.addfile(entry, io.BytesIO(data))
            print(
                f"Source transfer: {len(changed)} changed files, {len(removed)} removed files, {stream.tell()} compressed bytes",
                flush=True,
            )
            stream.seek(0)
            result = self.cancellation.run(
                self.command(
                    [
                        "sh",
                        "-c",
                        script,
                        "sync",
                        self.execution_root,
                        self.workspace,
                        str(reset).lower(),
                    ]
                ),
                stdin=stream,
            )
        if result.returncode:
            raise DeploymentError("source delta upload failed")
        print(
            f"Source transfer completed in {time.monotonic() - started:.1f}s",
            flush=True,
        )

    def export_application(
        self,
        component: str,
        destination: str,
        arguments: dict[str, str],
        parts: list[str] | None = None,
    ) -> None:
        """Export selected first-party application parts; callers assemble engine files separately."""
        if parts is None or "rust" in parts or "go" in parts:
            dockerfile = (
                "control-plane/Dockerfile" if component == "control-plane"
                else f"data-plane/{component}/Dockerfile"
            )
            # Runtime stages are outside the application export graph. Do not
            # materialize SDK images merely to supply their unused Dockerfile ARGs.
            arguments = {
                key: value for key, value in arguments.items()
                if not key.endswith("_IMAGE")
                or key in {"RUST_BUILDER_IMAGE", "GO_BUILDER_IMAGE", "BUILDKIT_SYNTAX_IMAGE"}
            }
            if component == "model-server":
                arguments["INFERENCE_ENGINE_IMAGE"] = "scratch"
            self.build(dockerfile, target="source-export", destination=destination, arguments=arguments)
            return
        local_output = self.execution_root + "/exports/" + uuid.uuid4().hex if self.execution_claim != self.claim else destination
        script = 'rm -rf "$1"; mkdir -p "$1"'
        if "python" in parts:
            script += (
                '; mkdir "$1/python"; '
                'if test -d "$2"; then cp -pR "$2/." "$1/python/"; fi'
            )
        self.run([
            "sh", "-ec", script, "export", local_output,
            self.workspace + "/data-plane/model-server/python",
        ])
        self._deliver_output(local_output, destination)

    def _deliver_output(self, source: str, destination: str) -> None:
        """Hand complete local exports to the persistent volume's ordinary writer."""
        self._share_output(source)
        if source == destination:
            return
        self.run([
            "sh", "-ec",
            'rm -rf "$2"; mkdir -p "$2"; cp -R --preserve=mode,timestamps --no-preserve=ownership "$1/." "$2/"',
            "deliver", source, destination,
        ], container=self.artifact_container)
        self.run(["rm", "-rf", "--", source])

    def _share_output(self, path: str) -> None:
        """Give the existing publisher group access to generated exports, not compiler snapshots."""
        self.run([
            "sh", "-ec", 'chgrp -Rh 1000 "$1"; chmod -R g+rX "$1"',
            "share", path,
        ])

    def resolve_image(self, image: str) -> str:
        """Resolve a build dependency through the same node store or registry used by BuildKit."""
        if image in self._resolved_images:
            return self._resolved_images[image]
        name = _image_reference(image)
        if name in self._generated_layouts:
            layout = self._generated_layouts[name]
            digest = layout["digest"]
            manifest = self.read_json(layout["path"] + "/blobs/" + self._context_digest(layout).replace(":", "/", 1))
            configuration = self.read_json(layout["path"] + "/blobs/" + manifest["config"]["digest"].replace(":", "/", 1))
            self._image_users[image] = configuration["config"].get("User", "") or "0"
        elif self.containerd_socket:
            store = NodeImageStore(self.kubectl, self.namespace, self.name, self.binding)
            digest = store.image_digest(name)
        else:
            digest = None
        if digest is None:
            # A FROM-only solve uses BuildKit's registry credentials, mirrors and
            # platform selection without running a container or adding image layers.
            dockerfile = ".foretoken-resolve/Dockerfile"
            self.run([
                "sh", "-ec",
                'mkdir -p "$1/.foretoken-resolve"; printf \'ARG BASE_IMAGE\\nFROM ${BASE_IMAGE}\\n\' > "$1/.foretoken-resolve/Dockerfile"',
                "resolve", self.workspace,
            ])
            try:
                metadata = self.build(
                    dockerfile, image="foretoken-resolve:" + uuid.uuid4().hex,
                    arguments={"BASE_IMAGE": image}, capture=True, metadata_only=True,
                )
            except DeploymentError as error:
                message = str(error)
                if "failed to resolve source metadata" in message and (
                    f"{image}: not found" in message or "404 Not Found" in message
                ):
                    raise ImageNotFoundError(message) from error
                raise
            digest = metadata["containerimage.digest"]
            configuration = json.loads(self.run(
                ["buildctl", "debug", "get", metadata["containerimage.config.digest"]],
                capture=True,
            ))
            self._image_users[image] = configuration["config"].get("User", "") or "0"
        self._resolved_images[image] = digest
        return digest

    def image_user(self, image: str) -> str:
        """Preserve the resolved runtime's USER when a dependency stage temporarily runs as root."""
        digest = self.resolve_image(image)
        if image not in self._image_users:
            machine = self.run(["uname", "-m"], capture=True).strip()
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}[machine]
            self._image_users[image] = NodeImageStore(
                self.kubectl, self.namespace, self.name, self.binding,
            ).image_user(digest, architecture)
        return self._image_users[image]

    def image_matches(self, image: str, digest: str) -> bool:
        """Verify a retained environment against its producer's OCI descriptor on this node."""
        if self.containerd_socket:
            return self._node_image_matches(image, digest)
        return self.resolve_image(image) == digest

    def _node_images(self) -> dict[str, str]:
        """Read node references for existing local build-context reuse."""
        return NodeImageStore(
            self.kubectl, self.namespace, self.name, self.binding
        ).images()

    def _node_image_matches(self, image: str, digest: str) -> bool:
        """Compare a selected node image with BuildKit output without exporting layers."""
        return NodeImageStore(
            self.kubectl, self.namespace, self.name, self.binding
        ).matches(image, digest)

    def _save_layout(self, image: str, layout: str, digest: str) -> None:
        """Retain the cluster-local OCI layout corresponding to an imported image reference."""
        path = self.root + "/images.json"
        layouts = self.read_json(path)
        previous = layouts.get(image)
        layouts[image] = {
            "path": layout,
            "digest": digest,
            "nodeDigest": self._node_images()[image],
        }
        encoded = shlex.quote(json.dumps(layouts))
        self.run(
            [
                "sh",
                "-ec",
                f'printf %s {encoded} > "$1.next"; mv "$1.next" "$1"',
                "save",
                path,
            ]
        )
        if previous and previous["path"] != layout:
            self.run(["rm", "-rf", "--", previous["path"]], container=self.artifact_container)

    def _local_image(self, image: str) -> dict[str, str] | None:
        """Expose built OCI outputs or existing node images as registry-free BuildKit contexts."""
        if not self.containerd_socket or image == "scratch":
            return None
        name = _image_reference(image)
        if name in self._generated_layouts:
            self._used_images.add(name)
            return self._generated_layouts[name]
        images = self._node_images()
        if name not in images:
            return None
        self._used_images.add(name)
        cached = self.read_json(self.root + "/images.json").get(name)
        if cached and cached["nodeDigest"] == images[name]:
            return cached
        identifier = uuid.uuid4().hex
        # Interrupted exports belong to the existing transient-output cleanup.
        layout = self.root + "/transfers/" + identifier
        self.run([
            "sh", "-ec", 'mkdir -p "$1"; chmod 2770 "$1"', "prepare", layout,
        ], container=self.artifact_container)
        machine = self.run(["uname", "-m"], capture=True).strip()
        platform = "linux/" + {"x86_64": "amd64", "aarch64": "arm64"}[machine]
        store = NodeImageStore(self.kubectl, self.namespace, self.name, self.binding)
        self.run(
            store.export_command(name, layout + "/image.tar", platform),
            container="images",
        )
        self.run(
            [
                "sh",
                "-ec",
                'tar --no-same-owner -xf "$1/image.tar" -C "$1"; rm "$1/image.tar"',
                "extract",
                layout,
            ],
            container=self.artifact_container if self.execution_claim != self.claim else "images",
        )
        digest = self.read_json(layout + "/index.json")["manifests"][0]["digest"]
        cached_path = self.root + "/images/" + identifier
        self.run(["mkdir", "-p", self.root + "/images"], container=self.artifact_container)
        self.run(["mv", layout, cached_path], container=self.artifact_container)
        self._save_layout(name, cached_path, digest)
        return {"path": cached_path, "digest": digest, "nodeDigest": images[name]}

    def _context_digest(self, layout: dict[str, str]) -> str:
        """Resolve a single-image index for BuildKit without changing export identities."""
        return image_manifest_digest(
            layout["digest"],
            lambda digest: self.read_json(layout["path"] + "/blobs/" + digest.replace(":", "/", 1)),
        )

    def build(
        self,
        dockerfile: str,
        *,
        target: str = "",
        destination: str = "",
        image: str = "",
        push: bool = False,
        arguments: dict[str, str] | None = None,
        capture: bool = False,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        """Build files or complete images, retaining local OCI outputs until delivery.

        Dependency resolution uses metadata_only to inspect a base without exporting its layers.
        """
        metadata = self.execution_root + "/results/" + uuid.uuid4().hex + ".json"
        args = [
            "buildctl",
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            "context=" + self.workspace,
            "--local",
            "dockerfile=" + self.workspace,
            "--opt",
            "filename=" + dockerfile,
            "--metadata-file",
            metadata,
            "--progress",
            "plain",
        ]
        if target:
            args += ["--opt", "target=" + target]
        syntax_image = (arguments or {}).get("BUILDKIT_SYNTAX_IMAGE")
        if syntax_image:
            source = self.workspace + "/" + dockerfile
            generated = (
                f"{self.workspace}/.foretoken-build/{uuid.uuid4().hex}/Dockerfile"
            )
            # BuildKit resolves Dockerfile-specific exclusions from the generated filename.
            self.run(
                [
                    "sh",
                    "-ec",
                    (
                        'mkdir -p "$(dirname "$3")"; first=$(head -n 1 "$2"); '
                        'if case "$first" in "# syntax="*) true;; *) false;; esac; then '
                        'tail -n +2 "$2" > "$3"; else cp "$2" "$3"; fi; '
                        'if test -f "$2.dockerignore"; then '
                        'cp "$2.dockerignore" "$3.dockerignore"; fi'
                    ),
                    "rewrite",
                    syntax_image,
                    source,
                    generated,
                ],
            )
            dockerfile = generated.removeprefix(self.workspace + "/")
            args[args.index("--opt") + 1] = "filename=" + dockerfile
        for key, value in (arguments or {}).items():
            local = None
            if key.endswith("_IMAGE"):
                # Local contexts share OCI exports and their index. Resolve them
                # atomically; independent buildctl solves run outside this lock.
                with self._image_lock:
                    local = self._local_image(value)
            if local:
                digest = self._context_digest(local)
                alias = "foretoken-local-" + key.lower().replace("_", "-")
                args += [
                    "--oci-layout",
                    alias + "=" + local["path"],
                    "--opt",
                    f"context:{alias}=oci-layout://{alias}@{digest}",
                ]
                value = alias
            args += ["--opt", f"build-arg:{key}={value}"]
        layout = ""
        local_output = ""
        if destination:
            local_output = self.execution_root + "/exports/" + uuid.uuid4().hex if self.execution_claim != self.claim else destination
            self.run(["mkdir", "-p", local_output])
            # BuildKit mirrors each owned export directory so retries cannot revive removed files.
            output = "type=local,mode=delete,dest=" + local_output
        elif metadata_only:
            output = f"type=image,name={image},oci-mediatypes=true,store=false,push=false"
        elif self.containerd_socket:
            # An exported layout owns the result independently of BuildKit's cache.
            # Replaying a completed solve for delivery can rebuild evicted dependency layers.
            layout = self.root + "/transfers/" + uuid.uuid4().hex
            local_output = self.execution_root + "/exports/" + uuid.uuid4().hex if self.execution_claim != self.claim else layout
            output = f"type=oci,name={image},oci-mediatypes=true,dest={local_output},tar=false"
        else:
            output = f"type=image,name={image},push={str(push).lower()}"
        started = time.monotonic()
        self.run([*args, "--output", output], capture=capture)
        if local_output:
            self._deliver_output(local_output, destination or layout)
        result = self.read_json(metadata)
        target_name = target or "default"
        output_name = destination or image or "files"
        print(
            f"Build completed: {dockerfile} target={target_name} output={output_name} "
            f"in {time.monotonic() - started:.1f}s",
            flush=True,
        )
        if layout:
            self._generated_layouts[_image_reference(image)] = {
                "path": layout, "digest": result["containerimage.digest"],
            }
        return result

    def import_image(self, image: str, reference: str) -> None:
        """Deliver a selected OCI output after source builds, reusing an identical node image."""
        image = _image_reference(image)
        layout = self._generated_layouts[image]
        digest = layout["digest"]
        if self._node_image_matches(reference, digest):
            print(f"Reusing unchanged node image {reference}", flush=True)
            NodeImageStore(
                self.kubectl, self.namespace, self.name, self.binding
            ).pin_owned(reference)
        else:
            NodeImageStore(
                self.kubectl, self.namespace, self.name, self.binding
            ).import_layout(layout["path"], reference)
            # Retire only references created by this operation after delivery.
            self._built_images.update((image, reference))
        if image in self._used_images:
            # A generated environment consumed by this build is also the next
            # iteration's input. Retain its immutable OCI files without copying
            # layer bytes or invalidating in-flight commands' original paths.
            cached = self.root + "/images/" + uuid.uuid4().hex
            self.run(["mkdir", "-p", self.root + "/images"], container=self.artifact_container)
            self.run(["cp", "-al", layout["path"], cached], container=self.artifact_container)
            self._save_layout(reference, cached, digest)
            self._used_images.add(reference)

    def discard_unselected_images(self, retained: set[str]) -> None:
        """Remove only this build's unselected local references after every node has been compared."""
        if self.containerd_socket:
            NodeImageStore(
                self.kubectl, self.namespace, self.name, self.binding
            ).remove_owned(self._built_images - retained)
