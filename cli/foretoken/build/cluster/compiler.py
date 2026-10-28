# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native compilers in private workspaces with platform-owned dependency caches."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from loguru import logger

from foretoken import DEFAULT_PLATFORM_REGISTRY, compiler_cache_reference, platform_version
from foretoken.build.cluster.artifacts import prepare_artifact_volume
from foretoken.build.cluster.builder import ClusterBuilder, ImageNotFoundError
from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
)
from foretoken.build.cluster.storage import SOURCE_BUILD_UID, ensure_shared_build_cache
from foretoken.build.cluster.transfer import synchronize_inputs
from foretoken.downloads.sources import platform_image_reference
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object
from foretoken.processes.execution import CommandExecution

APPLICATION_PARTS = {
    "control-plane": ("go",),
    "frontend": ("rust",),
    "model-server": ("rust", "python", "engine"),
}


@dataclass(frozen=True)
class CompilerCache:
    """Select a release cache image and any files staged for the current compiler operation."""

    image: str
    directory: str


class CompilerWorkspace(AbstractContextManager):
    """Own one operation's compiler processes while retaining its source and incremental state."""

    def __init__(
        self, kubectl: Kubectl, namespace: str, claim: str, binding: str, node: str,
        images: dict[str, str], configuration: dict[str, str], timeout: str, *,
        tools_image: str, owner: dict[str, str], execution_claim: str = "",
        pull_secrets: tuple[str, ...] = (), credentials: dict[str, Any] | None = None,
        cancellation: CommandExecution | None = None,
    ) -> None:
        """Bind private workspaces, artifact storage and shared caches; __enter__ allocates the compiler Pod."""
        self.kubectl = kubectl
        self.namespace = namespace
        self.claim = claim
        self.binding = binding
        self.node = node
        self.images = images
        self.configuration = configuration
        self.timeout = timeout
        self.tools_image = tools_image
        self.owner = owner
        self.pull_secrets = pull_secrets
        self.credentials = credentials or {}
        self.cancellation = cancellation or CommandExecution.current() or CommandExecution(timeout_seconds(timeout))
        self.name = "foretoken-compile-" + uuid.uuid4().hex[:12]
        self.mount = "/var/cache/foretoken"
        self.execution_claim = execution_claim or claim
        self.execution_mount = "/var/lib/foretoken/compiler" if self.execution_claim != claim else self.mount
        self.root = f"{self.mount}/build/{binding}/native"
        self.execution_root = f"{self.execution_mount}/build/{binding}/native"
        self.workspace = self.execution_root + "/inputs"
        self.shared_mount = "/var/lib/foretoken/dependencies"
        self.artifact_container = "artifacts" if self.execution_claim != claim else "builder"
        self.engine_name = self.name + "-engine"
        self._pods: dict[str, dict[str, Any] | None] = {}

    def __enter__(self) -> Self:
        """Start first-party compilers and initialize inputs and exports once for this operation."""
        self.cancellation.check()
        self.shared_claim = ensure_shared_build_cache(
            self.kubectl, self.namespace, self.node, self.configuration, self.owner,
        )
        self.artifacts_isolated = False
        if self.execution_claim != self.claim:
            self.artifacts_isolated = prepare_artifact_volume(
                self.kubectl, self.namespace, self.claim, self.binding, self.node,
                self.tools_image, self.pull_secrets, self.timeout, self.cancellation,
            )
        self.cancellation.register(self.retire)
        try:
            self._start_pod(self.name, self.images)
            self.run(["sh", "-ec", 'mkdir -p "$1"; rm -rf "$1/exports"; mkdir "$1/exports"', "prepare", self.execution_root])
            self.run(["sh", "-ec", 'mkdir -p "$1"; rm -rf "$1/output"; mkdir "$1/output"', "prepare", self.root], container=self.artifact_container)
            return self
        except BaseException as error:
            # Report the startup failure and stop peers before retirement can block.
            self.cancellation.cancel(error)
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> None:
        """Stop every compiler process before another operation can reuse this workspace."""
        try:
            self.cancellation.cleanup(self.retire, exc_value)
        finally:
            self.cancellation.unregister(self.retire)

    def sync(self, files: dict[str, Path], versions: dict[str, str]) -> None:
        """Capture a complete input tree separately from all mutable compiler workspaces."""
        synchronize_inputs(self.command, self.cancellation, self.workspace, self.execution_root + "/inputs.json", files, versions)

    def has_native_state(self, component: str) -> bool:
        """Report whether a component has initialized its native compiler workspace."""
        marker = "target" if "rust" in APPLICATION_PARTS[component] else "source.json"
        path = self.execution_root + "/work/" + component + "/" + marker
        return bool(self.run(["sh", "-ec", 'if test -e "$1"; then printf ready; fi', "cache", path], capture=True))

    def prepare_cache_seed(
        self, builder: ClusterBuilder, component: str, arguments: dict[str, str],
        *, reference: str | None = None,
    ) -> CompilerCache | None:
        """Resolve optional release state, extracting it only for a new private compiler workspace."""
        if self.has_native_state(component):
            return None
        reference = reference or compiler_cache_reference(DEFAULT_PLATFORM_REGISTRY, platform_version(), component)
        image = reference
        if mirror := arguments.get("UV_IMAGE_REGISTRY"):
            image = platform_image_reference(reference, mirror)
        try:
            builder.resolve_image(image)
        except ImageNotFoundError:
            return None
        destination = builder.execution_root + "/compiler-caches/" + component
        builder.run(["rm", "-rf", "--", destination])
        builder.extract_image(image, destination)
        return CompilerCache(reference, destination)

    def compile_application(
        self, component: str, arguments: dict[str, str], parts: list[str] | None = None,
        *, cache_seed: str | None = None,
    ) -> None:
        """Compile first-party parts into this operation's private export, retaining other completed parts."""
        output = self.execution_root + "/exports/" + component
        environment = {key: value for key, value in arguments.items() if key in {
            "GOPROXY", "GOSUMDB", "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_CARGO_REGISTRY",
            "CARGO_NET_GIT_FETCH_WITH_CLI", "FORETOKEN_MODEL_SERVER_BINARY",
        }}
        environment.update(
            CARGO_TARGET_DIR="/workspace/target",
            CARGO_HOME=self.shared_mount + "/cargo-home",
            GOMODCACHE=self.shared_mount + "/go-modules",
            GOCACHE=self.shared_mount + "/go-build",
        )
        if jobs := os.environ.get("BUILD_JOBS"):
            environment["BUILD_JOBS"] = jobs
        command = [
            "env", *(key + "=" + value for key, value in environment.items()),
            "python3", "-I", self.workspace + "/deploy/build-source", component, output,
            "--cache", self.shared_mount,
            "--workspace", "/workspace/source",
            "--versions", self.execution_root + "/inputs.json",
        ]
        if cache_seed is not None:
            command += ["--cache-seed", cache_seed]
        selected = [part for part in (parts if parts is not None else APPLICATION_PARTS[component]) if part != "engine"]
        if not selected:
            self.run(["mkdir", "-p", output])
        else:
            logger.info("Compiling {} ({}) on {}", component, ", ".join(selected), self.node)
            self.run([*command, "--parts", *selected], container=component)
            logger.info("Compilation completed: {}", component)

    def export_application(self, component: str, destination: str, *, engine: bool = False) -> None:
        """Deliver completed component files and its optional engine overlay for publication."""
        started = time.monotonic()
        logger.info("Exporting {} application files", component)
        self._deliver_output(self.execution_root + "/exports/" + component, destination)
        if engine:
            exported = destination + "-engine"
            self._deliver_output(self.execution_root + "/exports/engine", exported)
            self.run([
                "sh", "-ec", 'cp -R "$1/engine" "$2/"; cp "$1/engine-environment.json" "$2/"',
                "assemble", exported, destination,
            ], container=self.artifact_container)
        logger.info("{} application files exported in {:.1f}s", component, time.monotonic() - started)

    def compile_engine(self, image: str, arguments: dict[str, str], *, cache_id: str, build_native: bool) -> None:
        """Start the exact runtime's native compiler on the synchronized inputs and export its engine files."""
        self._start_pod(self.engine_name, {"engine": image})
        output = self.execution_root + "/exports/engine"
        environment = {
            "CCACHE_DIR": self.shared_mount + "/ccache",
            "CCACHE_BASEDIR": self.execution_root + "/engine/" + cache_id,
        }
        if jobs := os.environ.get("BUILD_JOBS"):
            environment["MAX_JOBS"] = jobs
        if architectures := arguments.get("TARGET_CUDA_ARCH_LIST"):
            environment["TORCH_CUDA_ARCH_LIST"] = architectures
        script = '''set -eu
if test -f /opt/foretoken-vllm/activate; then . /opt/foretoken-vllm/activate; fi
export PYTHONPATH=/opt/foretoken-engine-build/python${PYTHONPATH:+:$PYTHONPATH}
exec "${FORETOKEN_VLLM_PYTHON:-python3}" /opt/foretoken-engine-build/source-build.py "$@"
'''
        self.run([
            "env", *(key + "=" + value for key, value in environment.items()),
            "bash", "-ec", script, "engine-build", "--source-root", self.workspace + "/engine",
            "--cache", self.execution_root + "/engine/" + cache_id, "--output", output,
            *(["--build-native"] if build_native else []),
        ], container="engine")

    def command(self, args: list[str], *, container: str = "builder") -> list[str]:
        """Address this operation's first-party or engine Pod without changing publication identity."""
        name = self.engine_name if container == "engine" else self.name
        return self.kubectl.command(["exec", "-i", "-n", self.namespace, name, "-c", container, "--", *args])

    def run(self, args: list[str], *, container: str = "builder", capture: bool = False, input_text: str | None = None) -> str:
        """Run a compiler stage under the caller's cancellation scope with live diagnostics."""
        result = self.cancellation.run(
            self.command(args, container=container), text=True, capture_output=capture,
            input=input_text, stdout=None if capture else sys.stderr,
            stderr=None if capture else sys.stderr,
        )
        if result.returncode:
            self.cancellation.check()
            detail = (result.stderr or result.stdout or "").strip()
            name = self.engine_name if container == "engine" else self.name
            raise DeploymentError(f"compiler {self.namespace}/{name} ({container}) failed: {detail or result.returncode}")
        return result.stdout if capture else ""

    def retire(self) -> None:
        """Stop all operation-owned writers before releasing their private workspace."""
        with ExitStack() as cleanup:
            for name, metadata in tuple(self._pods.items()):
                cleanup.callback(self._retire_pod, name, metadata)

    def _start_pod(self, name: str, images: dict[str, str]) -> None:
        """Start one owned compiler Pod without resetting synchronized inputs or active exports."""
        self.cancellation.check()
        pod = self._pod(name, images)
        self._pods[name] = None
        created = self.kubectl.run(["create", "-f", "-", "-o", "json"], input_text=json.dumps(pod))
        metadata = json.loads(created.stdout)["metadata"]
        self._pods[name] = metadata
        self.cancellation.check()
        if self.credentials:
            self.kubectl.run(["create", "-f", "-"], input_text=json.dumps({
                "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
                "metadata": {
                    "name": name, "namespace": self.namespace,
                    "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": name, "uid": metadata["uid"]}],
                },
                "data": {".dockerconfigjson": base64.b64encode(json.dumps(self.credentials).encode()).decode()},
            }))
        deadline = time.monotonic() + timeout_seconds(self.timeout)
        wait_for_object(
            lambda: self.kubectl.get("pod", name, self.namespace, timeout=max(1.0, deadline - time.monotonic())),
            self._ready, deadline, f"compiler {self.namespace}/{name}",
        )

    def _retire_pod(self, name: str, observed: dict[str, Any] | None) -> None:
        """Delete and await a created compiler UID, leaving its shared caches and volumes intact."""
        current = self.kubectl.get_if_exists("pod", name, self.namespace)
        if current is None:
            return
        metadata = current["metadata"]
        if observed is not None:
            if metadata["uid"] != observed["uid"]:
                return
        elif not any(owner.get("uid") == self.owner["uid"] for owner in metadata.get("ownerReferences", [])):
            return
        try:
            self.kubectl.delete_object(current, self.timeout, match_version=False)
        except DeploymentError:
            current = self.kubectl.get_if_exists("pod", name, self.namespace)
            if current is not None and current["metadata"]["uid"] == metadata["uid"]:
                raise
            return

    def _ready(self, pod: dict[str, Any]) -> bool:
        """Surface startup failures while container diagnostics remain available."""
        self.cancellation.check()
        name = pod["metadata"]["name"]
        status = pod.get("status", {})
        for field in ("initContainerStatuses", "containerStatuses"):
            for container in status.get(field, []):
                terminated = container.get("state", {}).get("terminated")
                if terminated is not None and (field == "containerStatuses" or terminated["exitCode"]):
                    raise DeploymentError(f"compiler {name}: {container['name']}: {terminated.get('message') or terminated['reason']}")
        if status.get("phase") in {"Failed", "Succeeded"}:
            raise DeploymentError(f"compiler {name} stopped before becoming ready: {status.get('message') or status['phase']}")
        return any(condition["type"] == "Ready" and condition["status"] == "True" for condition in status.get("conditions", []))

    def _deliver_output(self, source: str, destination: str) -> None:
        """Copy only completed exports to the artifact writer, keeping native caches private."""
        self.run(["sh", "-ec", 'chmod -R g+rX "$1"', "share", source])
        self.run([
            "sh", "-ec", 'rm -rf "$2"; mkdir -p "$2"; cp -R --preserve=mode,timestamps --no-preserve=ownership "$1/." "$2/"',
            "deliver", source, destination,
        ], container=self.artifact_container)

    def _pod(self, pod_name: str, images: dict[str, str]) -> dict[str, Any]:
        """Project selected compilers onto the operation's private execution and shared dependency volumes."""
        isolated = self.artifacts_isolated
        execution_volume = "execution" if self.execution_claim != self.claim else "artifacts"
        volumes = [
            {"name": "artifacts", "persistentVolumeClaim": {"claimName": self.claim}},
            {"name": "dependencies", "persistentVolumeClaim": {"claimName": self.shared_claim}},
            {"name": "home", "emptyDir": {}},
        ]
        if execution_volume == "execution":
            volumes.append({"name": execution_volume, "persistentVolumeClaim": {"claimName": self.execution_claim}})
        mounts = [
            {"name": execution_volume, "mountPath": self.execution_mount},
            {"name": "dependencies", "mountPath": self.shared_mount},
            {"name": "home", "mountPath": "/home/foretoken"},
        ]
        security = {
            "runAsUser": SOURCE_BUILD_UID, "runAsGroup": 1000,
            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
        }
        workspaces = {
            name: self.execution_root + "/work/" + name
            for name in images if name in APPLICATION_PARTS
        }
        containers = [{
            "name": name, "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"],
            "workingDir": "/home/foretoken",
            "env": [{"name": "HOME", "value": "/home/foretoken"}, {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": security, "volumeMounts": list(mounts),
            "terminationMessagePolicy": "FallbackToLogsOnError",
        } for name, image in {"builder": self.tools_image, **images}.items()]
        # Stable compiler paths keep reusable build outputs independent of the
        # checkout's physical volume. Each subPath still selects its private tree.
        for container in containers:
            if directory := workspaces.get(container["name"]):
                container["volumeMounts"].append({
                    "name": execution_volume, "mountPath": "/workspace",
                    "subPath": directory.removeprefix(self.execution_mount + "/"),
                })
        if execution_volume == "execution":
            containers.append({
                "name": "artifacts", "image": self.tools_image, "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"],
                "securityContext": {**security, "runAsUser": 1000 if isolated else SOURCE_BUILD_UID},
                "volumeMounts": [
                    {"name": "artifacts", "mountPath": self.mount},
                    {"name": "execution", "mountPath": self.execution_mount, "readOnly": True},
                ],
            })
        prepare = '''set -eu
uid=$1; shift
for directory in "$@"; do
    mkdir -p "$directory"
    chown "$uid:1000" "$directory"
    chmod 2775 "$directory"
done
'''
        init = [{
            "name": "workspace", "image": self.tools_image,
            "command": ["sh", "-ec", prepare, "prepare", str(SOURCE_BUILD_UID), self.execution_root, self.shared_mount, "/home/foretoken", *workspaces.values()],
            "securityContext": {"runAsUser": 0}, "volumeMounts": mounts,
        }]
        if execution_volume == "execution":
            init.append({
                "name": "artifacts", "image": self.tools_image,
                "command": (
                    ["sh", "-ec", 'mkdir -p "$1"; chmod 2770 "$1"; test -w "$1"', "prepare", self.root]
                    if isolated else ["sh", "-ec", prepare, "prepare", str(SOURCE_BUILD_UID), self.mount, self.root]
                ),
                "securityContext": {"runAsUser": 1000 if isolated else 0, "runAsGroup": 1000},
                "volumeMounts": [{"name": "artifacts", "mountPath": self.mount}],
            })
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {
                "name": pod_name, "namespace": self.namespace, "ownerReferences": [self.owner],
                "labels": {SOURCE_BUILD_POD_LABEL: "true", SOURCE_BUILD_BINDING_LABEL: self.binding},
            },
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "imagePullSecrets": [{"name": name} for name in (*self.pull_secrets, *((pod_name,) if self.credentials else ()))],
                "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                    "matchFields": [{"key": "metadata.name", "operator": "In", "values": [self.node]}],
                }]}}},
                "tolerations": [{"key": key, "operator": "Exists", "effect": "NoSchedule"} for key in (
                    "node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master",
                )],
                "volumes": volumes, "initContainers": init, "containers": containers,
            },
        }
