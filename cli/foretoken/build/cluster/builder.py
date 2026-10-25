# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run cached source builds beside their outputs, transferring only changed inputs."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Self
from urllib.parse import urlsplit

from loguru import logger

from foretoken.build.cluster.artifacts import prepare_artifact_volume
from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
    NodeImageStore,
    images_container_spec,
    node_image_store,
)
from foretoken.build.cluster.native_client import (
    CLIENT_BINARY,
    client_compiler_image,
    client_module,
)
from foretoken.build.cluster.storage import SOURCE_BUILD_UID
from foretoken.build.cluster.transfer import synchronize_inputs
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object
from foretoken.processes.execution import CommandExecution, run_command

_BUILD_POD_LABEL = SOURCE_BUILD_POD_LABEL
_BUILD_BINDING_LABEL = SOURCE_BUILD_BINDING_LABEL


class ImageNotFoundError(DeploymentError):
    """Identify a collected registry artifact that its source producer can rebuild."""


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
        client_cache_claim: str = "",
        client_build_arguments: dict[str, str] | None = None,
        node: str = "",
        containerd_socket: str = "",
        pull_secrets: tuple[str, ...] = (),
        credentials: dict[str, Any] | None = None,
        registry_mirrors: dict[str, list[str]] | None = None,
        cancellation: CommandExecution | None = None,
    ) -> None:
        """Bind build paths and cancellation for the caller; __enter__ allocates the Pod."""
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
        self.client_cache_claim = client_cache_claim
        self.client_build_arguments = client_build_arguments or {}
        self.client_module = client_module() if containerd_socket else {}
        self.client_image = client_compiler_image(self.client_module, self.client_build_arguments, self.registry_mirrors) if containerd_socket else ""
        self.image_store = NodeImageStore(kubectl, namespace, self.name, binding)
        self.lease = ""
        self.platform = ""
        self._image_lock = Lock()
        self._retirement_lock = Lock()
        self._generated_images: dict[str, dict[str, Any]] = {}
        self._node_inputs: dict[str, dict[str, Any] | None] = {}
        self._resolved_images: dict[str, str] = {}
        self._image_users: dict[str, str] = {}

    def sync(self, files: dict[str, Path], versions: dict[str, str]) -> None:
        """Capture environment inputs using the same complete-inventory transfer as native builds."""
        synchronize_inputs(self.command, self.cancellation, self.workspace, self.execution_root + "/inputs.json", files, versions)
        # BuildKit includes directory modes in COPY cache keys. Clear inherited
        # volume permissions inside the inputs, leaving the private workspace root intact.
        self.run(["find", self.workspace, "-mindepth", "1", "-type", "d", "-exec", "chmod", "00755", "{}", "+"])

    def build(
        self,
        dockerfile: str,
        *,
        target: str = "",
        image: str = "",
        push: bool = False,
        arguments: dict[str, str] | None = None,
        capture: bool = False,
        metadata_only: bool = False,
        cache_imports: tuple[str, ...] = (),
        output_directory: str | None = None,
    ) -> dict[str, Any]:
        """Build an image or export files into this workspace, retaining image content until delivery.

        Dependency resolution uses metadata_only to inspect a base without exporting its layers.
        File exports run as the unprivileged builder and remain owned by the caller's workspace.
        """
        started = time.monotonic()
        # Concurrency belongs to this invocation, not a saved environment recipe.
        arguments = {key: value for key, value in (arguments or {}).items() if key != "BUILD_JOBS"}
        if jobs := os.environ.get("BUILD_JOBS"):
            arguments["BUILD_JOBS"] = jobs
        metadata = self.execution_root + "/results/" + uuid.uuid4().hex + ".json"
        frontend = {"filename": dockerfile}
        if target:
            frontend["target"] = target
        syntax_image = arguments.get("BUILDKIT_SYNTAX_IMAGE")
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
            frontend["filename"] = dockerfile
        inputs = {}
        for key, value in arguments.items():
            local = self._native_input(value) if key.endswith("_IMAGE") and output_directory is None else None
            if local is not None:
                alias = "foretoken-local-" + key.lower().replace("_", "-")
                inputs[alias] = local["descriptor"]
                value = alias
            frontend[f"build-arg:{key}"] = value
        if output_directory is not None:
            output = {"type": "local", "attrs": {"dest": output_directory}}
        elif metadata_only:
            output = {"type": "image", "attrs": {"name": image, "oci-mediatypes": "true", "store": "false", "push": "false"}}
        elif self.containerd_socket:
            output = {"type": "oci", "attrs": {"name": image, "oci-mediatypes": "true", "tar": "false"}}
        else:
            output = {"type": "image", "attrs": {"name": image, "push": str(push).lower()}}
        if self.containerd_socket and output_directory is None:
            request = {
                **self.image_store.client_request(self.lease, self.platform),
                "buildkit_address": "unix://" + self.runtime_root + "/runtime/buildkit/buildkitd.sock",
                "frontend_attrs": frontend, "inputs": inputs,
                "local_dirs": {"context": self.workspace, "dockerfile": self.workspace},
                "output": output, "metadata_file": metadata, "cache_imports": cache_imports,
            }
            self.run([CLIENT_BINARY, "solve"], container="images", input_text=json.dumps(request), capture=capture)
        else:
            args = [
                "buildctl", "build", "--frontend", "dockerfile.v0",
                "--local", "context=" + self.workspace, "--local", "dockerfile=" + self.workspace,
                "--metadata-file", metadata, "--progress", "plain",
                "--secret", "id=foretoken-build-jobs,env=BUILD_JOBS",
            ]
            transport = {key: arguments[key] for key in ("FORETOKEN_GITHUB_MIRROR", "GOPROXY") if key in arguments}
            for key in transport:
                args += ["--secret", f"id={key},env={key}"]
            for key, value in frontend.items():
                args += ["--opt", f"{key}={value}"]
            for reference in cache_imports:
                args += ["--import-cache", "type=registry,ref=" + reference]
            export = {"type": output["type"], **output["attrs"]}
            self.run(
                [*args, "--output", ",".join(f"{key}={value}" for key, value in export.items())],
                capture=capture, environment=transport,
            )
        result = self.read_json(metadata)
        logger.info(
            "Build completed: {} target={} output={} in {:.1f}s",
            dockerfile, target or "default", output_directory or image, time.monotonic() - started,
        )
        if self.containerd_socket and not metadata_only and output_directory is None:
            with self._image_lock:
                name = _image_reference(image)
                self._generated_images[name] = result
                self._node_inputs.pop(name, None)
        return result

    def extract_image(self, image: str, destination: str) -> None:
        """Export a registry image's files for compiler-cache initialization in this workspace."""
        self.build(
            self._image_dockerfile(), arguments={"BASE_IMAGE": image},
            output_directory=destination,
        )

    def _image_dockerfile(self) -> str:
        """Create an independent FROM-only projection for metadata or filesystem exports."""
        name = ".foretoken-resolve/" + uuid.uuid4().hex + "/Dockerfile"
        self.run([
            "sh", "-ec", 'mkdir -p "$(dirname "$1")"; printf \'ARG BASE_IMAGE\\nFROM ${BASE_IMAGE}\\n\' > "$1"',
            "resolve", self.workspace + "/" + name,
        ])
        return name

    def resolve_image(self, image: str) -> str:
        """Resolve a build dependency through the same node store or registry used by BuildKit."""
        if image in self._resolved_images:
            return self._resolved_images[image]
        local = self._native_input(image)
        digest = local["descriptor"]["digest"] if local is not None else None
        if local is not None:
            self._image_users[image] = local["user"]
        if digest is None:
            # A FROM-only solve uses BuildKit's registry credentials, mirrors and
            # platform selection without running a container or adding image layers.
            dockerfile = self._image_dockerfile()
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
        self.resolve_image(image)
        return self._image_users[image]

    def image_matches(self, image: str, digest: str) -> bool:
        """Verify a retained environment against its producer's OCI descriptor on this node."""
        if self.containerd_socket:
            return self.image_store.matches(image, digest)
        return self.resolve_image(image) == digest

    def import_image(self, image: str, reference: str) -> None:
        """Publish a selected native result after source builds, reusing an identical node image."""
        image = _image_reference(image)
        metadata = self._generated_images[image]
        if self.image_store.matches(reference, metadata["containerimage.digest"]):
            logger.info("Reusing unchanged node image {}", reference)
            self.image_store.pin_owned(reference)
        else:
            self.image_store.deliver(metadata["containerimage.descriptor"], reference, self.lease, self.platform)

    def __enter__(self) -> Self:
        """Start this operation's image builder on its persistent cache node."""
        self.cancellation.check()
        self.cancellation.register(self.retire)
        artifact_nfs_isolated = False
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
        logger.info("Preparing cluster builder {}/{}", self.namespace, self.name)
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
            pod = self._pod(artifact_nfs_isolated=artifact_nfs_isolated)
            self.cancellation.check()
            created = self.kubectl.run(
                ["create", "-f", "-", "-o", "json"], input_text=json.dumps(pod)
            )
            self.cancellation.check()
            pod_uid = json.loads(created.stdout)["metadata"]["uid"]
            if self.client_module:
                self.kubectl.run(
                    ["create", "-f", "-"],
                    input_text=json.dumps({
                        "apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {
                            "name": self.name, "namespace": self.namespace,
                            "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": self.name, "uid": pod_uid}],
                        },
                        "data": self.client_module,
                    }),
                )
            if self.credentials:
                self.kubectl.run(
                    ["create", "-f", "-"],
                    input_text=json.dumps(self._registry_secret(pod_uid)),
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
            # Report the startup failure and stop peers before retirement can block.
            self.cancellation.cancel(error)
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> None:
        """Retain completed cache files, then stop this operation's daemon and executor processes."""
        try:
            if exc_type is None:
                self._retain_build_cache()
        finally:
            try:
                self.cancellation.cleanup(self.retire, exc_value or sys.exception())
            finally:
                self.cancellation.unregister(self.retire)

    def retire(self) -> None:
        """Join this operation's writers before releasing its native content lease."""
        with self._retirement_lock:
            self.kubectl.run([
                "delete", "pod", self.name, "-n", self.namespace,
                "--ignore-not-found", "--wait=true", "--timeout=" + self.timeout,
            ])
            if self.lease:
                with node_image_store(
                    self.kubectl, self.namespace, self.node, self.containerd_socket,
                    self.tools_image, self.timeout, binding=self.binding, pull_secrets=self.pull_secrets,
                ) as store:
                    store.release_leases({self.lease})
                self.lease = ""

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
        environment: dict[str, str] | None = None,
    ) -> str:
        """Execute a build stage, preserving live compiler and daemon diagnostics on failure."""
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        command = ["env", *(f"{key}={value}" for key, value in environment.items()), *args] if environment else args
        result = self.cancellation.run(
            self.command(command, container=container),
            text=True,
            capture_output=capture,
            input=input_text,
            stdout=None if capture else sys.stderr,
            stderr=None if capture else sys.stderr,
        )
        if result.returncode:
            self.cancellation.check()
            detail = (result.stderr or result.stdout or "").strip()
            if (container == "builder" and args[0] == "buildctl") or (
                container == "images" and args[:2] == [CLIENT_BINARY, "solve"]
            ):
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
                    logger.warning("Builder logs unavailable: {}", error)
                else:
                    if logs.returncode == 0 and logs.stdout.strip():
                        logger.info("{}", logs.stdout)
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
                        logger.warning("Builder logs unavailable: {}", logs.stderr.strip())
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

    def _pod(self, *, artifact_nfs_isolated: bool) -> dict[str, Any]:
        """Render the daemon's local execution, artifact and image-import resources for startup."""
        image = self.image
        pvc = self.kubectl.get("pvc", self.execution_claim, self.namespace)
        capacity = (
            pvc.get("status", {})
            .get("capacity", {})
            .get("storage", pvc["spec"]["resources"]["requests"]["storage"])
        )
        gc_limits = _cache_gc_limits(capacity)
        artifact_uid = 1000 if artifact_nfs_isolated else SOURCE_BUILD_UID
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
for directory in workspace results; do
    mkdir -p "$persistent/$directory"
    chown "$uid:1000" "$persistent/$directory"
    chmod 2770 "$persistent/$directory"
done
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
                {"name": "BUILD_JOBS", "value": os.environ.get("BUILD_JOBS", "")},
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
                    "runAsUser": artifact_uid,
                    "runAsGroup": 1000,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": "cache", "mountPath": self.mount}],
            })
        if self.containerd_socket:
            # The installed CLI supplies this client. Editable workloads cannot
            # write its source, binary or compiler cache, or reach the node socket.
            client_directory = str(Path(CLIENT_BINARY).parent)
            client_source = "/opt/foretoken-build-client-source"
            client_cache = "/var/cache/foretoken-build-client"
            go_flags = "-mod=readonly -modcacherw"
            if jobs := os.environ.get("BUILD_JOBS"):
                go_flags += " -p=" + jobs
            spec["volumes"] += [
                {"name": "client-source", "configMap": {"name": self.name}},
                {"name": "client-binary", "emptyDir": {}},
                {"name": "client-cache", "persistentVolumeClaim": {"claimName": self.client_cache_claim}},
            ]
            spec["initContainers"].append({
                "name": "build-client", "image": self.client_image,
                "workingDir": client_source,
                "terminationMessagePolicy": "FallbackToLogsOnError",
                "command": [
                    "sh", "-ec",
                    (
                        'mkdir -p "$TMPDIR"; go build -trimpath -o "$1" .; '
                        'cp /etc/ssl/certs/ca-certificates.crt "$2/ca-certificates.crt"; rm -rf "$TMPDIR"'
                    ),
                    "compile", CLIENT_BINARY, client_directory,
                ],
                "env": [
                    {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                    {"name": "CGO_ENABLED", "value": "0"},
                    {"name": "GOTOOLCHAIN", "value": "local"},
                    {"name": "GOWORK", "value": "off"},
                    {"name": "GOFLAGS", "value": go_flags},
                    {"name": "GOCACHE", "value": client_cache + "/build"},
                    {"name": "GOMODCACHE", "value": client_cache + "/modules"},
                    {"name": "TMPDIR", "value": client_directory + "/tmp"},
                    *({"name": key, "value": value} for key, value in self.client_build_arguments.items() if key in {"GOPROXY", "GOSUMDB"}),
                ],
                "securityContext": {
                    "runAsUser": 0, "runAsGroup": 1000,
                    "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "client-source", "mountPath": client_source, "readOnly": True},
                    {"name": "client-binary", "mountPath": client_directory},
                    {"name": "client-cache", "mountPath": client_cache},
                ],
            })
            images, volumes = images_container_spec(self.tools_image, self.containerd_socket)
            images["securityContext"]["runAsUser"] = SOURCE_BUILD_UID
            # Native registry authentication needs a writable home inside this
            # container, separate from executor state and read-only credentials.
            images["env"] += [
                {"name": "HOME", "value": "/tmp"},
                {"name": "SSL_CERT_FILE", "value": client_directory + "/ca-certificates.crt"},
            ]
            images["volumeMounts"] += [
                {"name": "client-binary", "mountPath": client_directory, "readOnly": True},
                {"name": "runtime", "mountPath": self.runtime_root + "/runtime", "subPath": "runtime"},
                *({
                    "name": "execution" if self.execution_claim != self.claim else "cache",
                    "mountPath": self.execution_root + "/" + directory,
                    "subPath": f"build/{self.binding}/{directory}",
                    "readOnly": directory == "workspace",
                } for directory in ("workspace", "results")),
            ]
            if self.credentials:
                images["volumeMounts"].append({"name": "registry-auth", "mountPath": "/registry-auth", "readOnly": True})
                images["env"].append({"name": "DOCKER_CONFIG", "value": "/registry-auth"})
            spec["volumes"] += volumes
            spec["containers"].append(images)
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "ownerReferences": [{
                    "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "name": self.execution_claim, "uid": pvc["metadata"]["uid"],
                }],
                "labels": {
                    _BUILD_POD_LABEL: "true",
                    _BUILD_BINDING_LABEL: self.binding,
                },
            },
            "spec": spec,
        }
        return pod

    def _registry_secret(self, pod_uid: str) -> dict[str, Any]:
        """Render credentials owned by the acknowledged daemon Pod so native GC retires them together."""
        return {
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

    def _prepare_outputs(self) -> None:
        """Retire obsolete OCI copies and leases after the checkout owner joins earlier writers."""
        if self.containerd_socket:
            self.image_store.release_leases()
            self.lease = self.name
            machine = self.run(["uname", "-m"], capture=True).strip()
            self.platform = "linux/" + {"x86_64": "amd64", "aarch64": "arm64"}[machine]
        self.run([
            "sh", "-ec", 'rm -rf -- "$1/transfers" "$1/output" "$1/images" "$1/images.json"',
            "prepare", self.root,
        ], container=self.artifact_container)
        self._retain_build_cache()

    def _retain_build_cache(self) -> None:
        """Clear solve metadata without replacing the directory mounted by the trusted client."""
        self.run([
            "sh", "-ec",
            'rm -rf -- "$1/exports"; mkdir -p "$1/results"; find "$1/results" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +',
            "prepare", self.execution_root,
        ])

    def _native_input(self, image: str) -> dict[str, Any] | None:
        """Retain a selected node or generated input once while independent solves share its read-only graph."""
        if not self.containerd_socket or image == "scratch":
            return None
        name = _image_reference(image)
        with self._image_lock:
            if name not in self._node_inputs:
                generated = self._generated_images.get(name)
                self._node_inputs[name] = self.image_store.inspect(
                    name, self.lease, self.platform,
                    descriptor=generated["containerimage.descriptor"] if generated is not None else None,
                )
            return self._node_inputs[name]


def remove_build_pods(kubectl: Kubectl, timeout: str, *, binding: str) -> None:
    """Stop an earlier operation's processes before the binding reuses its persistent compiler state."""
    kubectl.run([
        "delete", "pods", "--all-namespaces", "--selector",
        f"{_BUILD_POD_LABEL}=true,{_BUILD_BINDING_LABEL}={binding}",
        "--ignore-not-found", "--wait=true", "--timeout=" + timeout,
    ])


def registry_credentials(
    images: Iterable[str], *, registries: Iterable[str] = (),
) -> dict[str, Any]:
    """Resolve native Docker login helpers for this build's images and registry prefixes."""
    path = (
        Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        / "config.json"
    )
    if not path.is_file():
        return {}
    configuration = json.loads(path.read_text())
    hosts = {
        _registry_host(_image_reference(image).split("/", 1)[0])
        for image in images if image
    }
    hosts.update(_registry_host(registry) for registry in registries if registry)
    entries = configuration.get("auths", {})
    helpers = configuration.get("credHelpers", {})
    auths = {}
    for host in sorted(hosts):
        key = "https://index.docker.io/v1/" if host == "docker.io" else host
        helper = _registry_entry(helpers, host) or configuration.get("credsStore")
        if helper:
            try:
                result = run_command(
                    ["docker-credential-" + helper, "get"],
                    input=key,
                    text=True,
                    capture_output=True,
                )
            except FileNotFoundError as error:
                raise DeploymentError(f"Docker credential helper {helper!r} is not installed") from error
            if result.returncode:
                # Native helpers use this sentinel for an anonymous registry;
                # other failures must not be mistaken for missing credentials.
                detail = result.stdout.strip() or result.stderr.strip()
                if result.returncode != 1 or detail != "credentials not found in native keychain":
                    raise DeploymentError(f"could not read login credentials for {host}")
            else:
                credentials = json.loads(result.stdout)
                if credentials["Username"] == "<token>":
                    auths[host] = {"identitytoken": credentials["Secret"]}
                else:
                    token = f"{credentials['Username']}:{credentials['Secret']}".encode()
                    auths[host] = {"auth": base64.b64encode(token).decode()}
                continue
        entry = entries[key] if key in entries else _registry_entry(entries, host)
        if entry:
            auths[host] = entry
    return {"auths": auths} if auths else {}


def _registry_entry(entries: dict[str, Any], host: str) -> Any:
    """Prefer exact registry configuration before accepting an equivalent URL or Hub alias."""
    if host in entries:
        return entries[host]
    return next((value for key, value in entries.items() if _registry_host(key) == host), None)


def _registry_host(value: str) -> str:
    """Match Docker config URL keys and Hub aliases to their registry authority."""
    host = urlsplit(value if "://" in value else "//" + value).netloc.lower()
    return "docker.io" if host in {"docker.io", "index.docker.io", "registry-1.docker.io"} else host


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


def _cache_gc_limits(quantity: str) -> tuple[str, str, str]:
    """Apply the pinned BuildKit defaults to the execution-volume budget, not the whole host disk."""
    from kubernetes.utils.quantity import parse_quantity

    try:
        capacity = int(parse_quantity(quantity))
    except ValueError as error:
        raise DeploymentError(f"invalid compiler volume capacity: {quantity}") from error
    # BuildKit 0.33 reserves 10% (up to 10 GB), keeps 20% free, and uses at most
    # 80% (up to 100 GB). Explicit image configuration remains authoritative.
    return (
        str(min(capacity // 10, 10_000_000_000)),
        str(capacity // 5),
        str(min(capacity * 4 // 5, 100_000_000_000)),
    )
