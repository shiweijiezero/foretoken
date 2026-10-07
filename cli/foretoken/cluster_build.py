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
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import yaml

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError

_BUILD_POD_LABEL = "inference.foretoken.io/source-builder"
_BUILD_BINDING_LABEL = "inference.foretoken.io/source-build-binding"


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
            result = subprocess.run(
                ["docker-credential-" + helper, "get"],
                input=key,
                text=True,
                capture_output=True,
                check=False,
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
    """Apply the pinned BuildKit defaults to its PVC allocation instead of a shared host disk."""
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
        node: str = "",
        containerd_socket: str = "",
        pull_secrets: tuple[str, ...] = (),
        credentials: dict[str, Any] | None = None,
    ) -> None:
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
        self.containerd_client = "/usr/local/bin/ctr"
        self.pull_secrets = pull_secrets
        self.credentials = credentials or {}
        self.name = "foretoken-build-" + uuid.uuid4().hex[:12]
        self.root = f"{self.mount}/build/{binding}"
        self.workspace = f"{self.root}/workspace"
        self._created = False
        self._generated_layouts: dict[str, dict[str, str]] = {}
        self._used_images: set[str] = set()
        self._built_images: set[str] = set()

    def _reuse_or_retire_pods(self) -> dict[str, Any] | None:
        """Select a compatible daemon and retire other builders sharing its compiler cache."""
        selector = f"{_BUILD_POD_LABEL}=true,{_BUILD_BINDING_LABEL}={self.binding}"
        occupants = []
        candidates = []
        for pod in self.kubectl.list_resources(
            ("pods",), self.namespace, label_selector=selector
        ):
            if not any(
                volume["name"] == "cache"
                and volume.get("persistentVolumeClaim", {}).get("claimName")
                == self.claim
                for volume in pod["spec"].get("volumes", [])
            ):
                continue
            occupants.append(pod)
            if not _pod_ready(pod) or pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            if self.node and pod.get("spec", {}).get("nodeName") != self.node:
                continue
            containers = pod.get("spec", {}).get("containers", [])
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
        # Source operations hold the cluster lock. Stop the previous daemon before
        # changing its image or placement: the retained PVC still holds its root lock.
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
        reusable = self._reuse_or_retire_pods()
        if reusable is not None:
            self.name = reusable["metadata"]["name"]
            print(f"Reusing cluster builder {self.namespace}/{self.name}", flush=True)
            self.run(
                ["rm", "-rf", "--", self.root + "/transfers", self.root + "/output"]
            )
            return self
        image = self.image
        pvc = self.kubectl.get("pvc", self.claim, self.namespace)
        capacity = (
            pvc.get("status", {})
            .get("capacity", {})
            .get("storage", pvc["spec"]["resources"]["requests"]["storage"])
        )
        gc_limits = _cache_gc_limits(capacity)
        spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "hostNetwork": True,
            "dnsPolicy": "Default",
            "automountServiceAccountToken": False,
            "tolerations": [
                {"key": key, "operator": "Exists", "effect": "NoSchedule"}
                for key in (
                    "node-role.kubernetes.io/control-plane",
                    "node-role.kubernetes.io/master",
                )
            ],
            "imagePullSecrets": [{"name": name} for name in self.pull_secrets],
            "volumes": [
                {"name": "cache", "persistentVolumeClaim": {"claimName": self.claim}}
            ],
            "initContainers": [
                {
                    "name": "workspace",
                    "image": image,
                    "command": [
                        "sh",
                        "-ec",
                        'mount="$(dirname "$(dirname "$1")")"; mkdir -p "$mount" "$1"; chown 1000:1000 "$mount" "$mount/build" "$(dirname "$1")" "$1"; chmod 2775 "$mount" "$mount/build" "$(dirname "$1")" "$1"; configuration="${XDG_CONFIG_HOME:-$HOME/.config}/buildkit/buildkitd.toml"; if test -f "$configuration"; then cp "$configuration" "$1/buildkit.toml"; else printf "[worker.oci]\\nreservedSpace = %s\\nminFreeSpace = %s\\nmaxUsedSpace = %s\\n" "$2" "$3" "$4" > "$1/buildkit.toml"; fi; chown 1000:1000 "$1/buildkit.toml"; chmod 600 "$1/buildkit.toml"',
                        "prepare",
                        self.root,
                        *gc_limits,
                    ],
                    "securityContext": {"runAsUser": 0},
                    "volumeMounts": [{"name": "cache", "mountPath": self.mount}],
                }
            ],
        }
        container: dict[str, Any] = {
            "name": "builder",
            "image": image,
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "args": [
                "--root",
                f"{self.root}/buildkit",
                "--config",
                f"{self.root}/buildkit.toml",
            ],
            "volumeMounts": [{"name": "cache", "mountPath": self.mount}],
            "readinessProbe": {
                "exec": {"command": ["buildctl", "debug", "workers"]},
                "periodSeconds": 2,
            },
        }
        container["args"].append("--oci-worker-no-process-sandbox")
        container["securityContext"] = {
            "runAsUser": 1000,
            "runAsGroup": 1000,
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
        spec["containers"] = [container]
        # PID 1 must handle termination rather than consume the Pod's grace period.
        idle_command = ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"]
        if self.containerd_socket:
            # Only local kind/k3d installation uses node image import. The compiler
            # never receives the runtime socket; archives remain on the cluster volume.
            # GNU tar preserves PAX sizes for OCI layer blobs larger than 8 GiB.
            client = (
                "/bin/k3s"
                if "/k3s/" in self.containerd_socket
                else "/usr/local/bin/ctr"
            )
            spec["volumes"] += [
                {
                    "name": "runtime-socket",
                    "hostPath": {"path": self.containerd_socket, "type": "Socket"},
                },
                {
                    "name": "runtime-client",
                    "hostPath": {"path": client, "type": "File"},
                },
            ]
            spec["containers"].append(
                {
                    "name": "images",
                    "image": self.tools_image,
                    "command": idle_command,
                    "env": [
                        {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                    ],
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsGroup": 0,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": "cache", "mountPath": self.mount},
                        {
                            "name": "runtime-socket",
                            "mountPath": "/run/foretoken-containerd.sock",
                        },
                        {
                            "name": "runtime-client",
                            "mountPath": self.containerd_client,
                            "readOnly": True,
                        },
                    ],
                }
            )
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {
                    _BUILD_POD_LABEL: "true",
                    _BUILD_BINDING_LABEL: self.binding,
                },
            },
            "spec": spec,
        }
        print(f"Preparing cluster builder {self.namespace}/{self.name}", flush=True)
        try:
            created = self.kubectl.run(
                ["create", "-f", "-", "-o", "json"], input_text=yaml.safe_dump(pod)
            )
            self._created = True
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
            self.kubectl.run(
                [
                    "wait",
                    "pod/" + self.name,
                    "-n",
                    self.namespace,
                    "--for=condition=Ready",
                    "--timeout=" + self.timeout,
                ]
            )
            self.run(
                ["rm", "-rf", "--", self.root + "/transfers", self.root + "/output"]
            )
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Retain successful builders and clean transient or failed build state."""
        if exc_type is not None and self._created:
            self.kubectl.run(
                [
                    "delete",
                    "pod",
                    self.name,
                    "-n",
                    self.namespace,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=" + self.timeout,
                ]
            )
            self._created = False
            return
        if exc_type is None and self._used_images:
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
                self.run(["rm", "-rf", "--", *stale])
        if exc_type is None and self._generated_layouts:
            self.run(["rm", "-rf", "--", self.root + "/transfers"])

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
        """Execute a build stage, preserving live compiler diagnostics by default."""
        result = subprocess.run(
            self.command(args, container=container),
            check=False,
            text=True,
            capture_output=capture,
            input=input_text,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise DeploymentError(
                f"cluster build failed in {self.namespace}/{self.name}: {detail or result.returncode}"
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
        marker = self.root + "/inputs.json"
        previous = self.read_json(marker)
        changed = [name for name in files if previous.get(name) != versions[name]]
        removed = sorted(previous.keys() - versions.keys())
        if not changed and not removed:
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
            result = subprocess.run(
                self.command(
                    [
                        "sh",
                        "-c",
                        script,
                        "sync",
                        self.root,
                        self.workspace,
                        str(reset).lower(),
                    ]
                ),
                stdin=stream,
                check=False,
            )
        if result.returncode:
            raise DeploymentError("source delta upload failed")

    def _containerd(self) -> list[str]:
        """Address the local development node's image store through its own client."""
        return [
            self.containerd_client,
            "--address",
            "/run/foretoken-containerd.sock",
            "--namespace",
            "k8s.io",
        ]

    def _node_images(self) -> dict[str, str]:
        """Read containerd's image reference and OCI digest columns for local base reuse."""
        output = self.run(
            [*self._containerd(), "images", "list"], capture=True, container="images"
        )
        return {
            columns[0]: columns[2]
            for line in output.splitlines()[1:]
            if len(columns := line.split()) >= 3
        }

    def _node_image_matches(self, image: str, digest: str) -> bool:
        """Compare a node image with BuildKit's output, including containerd's OCI index wrapper."""
        node_digest = self._node_images().get(image)
        if node_digest is None:
            return False
        if node_digest == digest:
            return True
        descriptor = json.loads(
            self.run(
                [*self._containerd(), "content", "get", node_digest],
                capture=True,
                container="images",
            )
        )
        manifests = descriptor.get("manifests", [])
        return len(manifests) == 1 and manifests[0]["digest"] == digest

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
            self.run(["rm", "-rf", "--", previous["path"]])

    def _local_image(self, image: str) -> dict[str, str] | None:
        """Expose an already-loaded local image as a BuildKit OCI context without a registry."""
        if not self.containerd_socket:
            return None
        name = image
        first = name.split("/", 1)[0]
        if "/" not in name or not (
            "." in first or ":" in first or first == "localhost"
        ):
            name = "docker.io/" + ("library/" if "/" not in name else "") + name
        if ":" not in name.rsplit("/", 1)[-1] and "@" not in name:
            name += ":latest"
        if name in self._generated_layouts:
            return self._generated_layouts[name]
        images = self._node_images()
        if name not in images:
            return None
        self._used_images.add(name)
        cached = self.read_json(self.root + "/images.json").get(name)
        if cached and cached["nodeDigest"] == images[name]:
            return cached
        layout = self.root + "/images/" + uuid.uuid4().hex
        self.run(["mkdir", "-p", layout])
        machine = self.run(["uname", "-m"], capture=True).strip()
        platform = "linux/" + {"x86_64": "amd64", "aarch64": "arm64"}[machine]
        self.run(
            [
                *self._containerd(),
                "images",
                "export",
                "--platform",
                platform,
                layout + "/image.tar",
                name,
            ],
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
            container="images",
        )
        digest = self.read_json(layout + "/index.json")["manifests"][0]["digest"]
        self._save_layout(name, layout, digest)
        return {"path": layout, "digest": digest, "nodeDigest": images[name]}

    def build(
        self,
        dockerfile: str,
        *,
        target: str = "",
        destination: str = "",
        image: str = "",
        push: bool = False,
        arguments: dict[str, str] | None = None,
        reuse_image: str = "",
    ) -> dict[str, Any]:
        """Build cluster-local outputs, reusing identical installed images before OCI transfer."""
        metadata = self.root + "/result.json"
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
            local = self._local_image(value) if key.endswith("_IMAGE") else None
            if local:
                alias = "foretoken-local-" + key.lower().replace("_", "-")
                args += [
                    "--oci-layout",
                    alias + "=" + local["path"],
                    "--opt",
                    f"context:{alias}=oci-layout://{alias}@{local['digest']}",
                ]
                value = alias
            args += ["--opt", f"build-arg:{key}={value}"]
        # Resolve the actual image through BuildKit without copying its layers to an
        # OCI layout. Only a matching image still present on this node can skip import.
        if self.containerd_socket and reuse_image and not destination:
            self.run(
                [
                    *args,
                    "--output",
                    f"type=image,name={image},oci-mediatypes=true,store=false,push=false",
                ]
            )
            result = self.read_json(metadata)
            if self._node_image_matches(reuse_image, result["containerimage.digest"]):
                self.reuse_image_reference(reuse_image, image)
                self._built_images.add(image)
                print(f"Reusing unchanged node image {reuse_image}", flush=True)
                return result
        layout = ""
        if destination:
            # BuildKit mirrors each owned export directory so retries cannot revive removed files.
            args += ["--output", "type=local,mode=delete,dest=" + destination]
        elif self.containerd_socket:
            layout = self.root + "/transfers/" + uuid.uuid4().hex
            args += ["--output", f"type=oci,name={image},dest={layout},tar=false"]
        else:
            args += ["--output", f"type=image,name={image},push={str(push).lower()}"]
        self.run(args)
        result = self.read_json(metadata)
        if layout:
            import_command = [*self._containerd(), "images", "import"]
            # ctr 1.x imports synchronously by default. On ctr 2.x select that same
            # path instead of the transfer service's temporary image references.
            help_text = self.run(
                [*import_command, "--help"], capture=True, container="images"
            )
            if "--local" in help_text:
                import_command.append("--local")
            self.run(
                [
                    "bash",
                    "-ec",
                    'set -o pipefail; layout=$1; shift; tar -C "$layout" -cf - . | "$@"',
                    "import",
                    layout,
                    *import_command,
                    "--index-name",
                    image,
                    "-",
                ],
                container="images",
            )
            digest = self.read_json(layout + "/index.json")["manifests"][0]["digest"]
            self._generated_layouts[image] = {
                "path": layout,
                "digest": digest,
                "nodeDigest": self._node_images()[image],
            }
            self._built_images.add(image)
        return result

    def reuse_image_reference(self, image: str, reference: str) -> None:
        """Restore an unchanged local installation reference without exporting its layers again."""
        self.run(
            [*self._containerd(), "images", "tag", "--force", image, reference],
            container="images",
        )

    def discard_unselected_images(self, retained: set[str]) -> None:
        """Remove only this build's unselected local references after every node has been compared."""
        if self.containerd_socket:
            unused = self._built_images - retained
            if unused:
                self.run(
                    [*self._containerd(), "images", "remove", *sorted(unused)],
                    container="images",
                )
