# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native compilers in private workspaces with platform-owned dependency caches."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Self

from foretoken.build.cluster.artifacts import prepare_artifact_volume
from foretoken.build.cluster.images import (
    SOURCE_BUILD_BINDING_LABEL,
    SOURCE_BUILD_POD_LABEL,
)
from foretoken.build.cluster.storage import SOURCE_BUILD_UID, ensure_shared_build_cache
from foretoken.build.cluster.transfer import synchronize_inputs
from foretoken.command_execution import CommandExecution
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object


class CompilerWorkspace(AbstractContextManager):
    """Own one operation's compiler processes while retaining its source and incremental state."""

    def __init__(
        self, kubectl: Kubectl, namespace: str, claim: str, binding: str, node: str,
        images: dict[str, str], configuration: dict[str, str], timeout: str, *,
        tools_image: str, owner: dict[str, str], execution_claim: str = "",
        pull_secrets: tuple[str, ...] = (), credentials: dict[str, Any] | None = None,
        cancellation: CommandExecution | None = None,
    ) -> None:
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
        self.metadata: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        """Start isolated compiler containers after resolving their persistent local storage."""
        self.cancellation.check()
        shared = ensure_shared_build_cache(
            self.kubectl, self.namespace, self.node, self.configuration, self.owner,
        )
        isolated = False
        if self.execution_claim != self.claim:
            isolated = prepare_artifact_volume(
                self.kubectl, self.namespace, self.claim, self.binding, self.node,
                self.tools_image, self.pull_secrets, self.timeout, self.cancellation,
            )
        pod = self._pod(shared, isolated)
        self.cancellation.register(self.retire)
        try:
            created = self.kubectl.run(["create", "-f", "-", "-o", "json"], input_text=json.dumps(pod))
            self.metadata = json.loads(created.stdout)["metadata"]
            self.cancellation.check()
            if self.credentials:
                self.kubectl.run(["create", "-f", "-"], input_text=json.dumps({
                    "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
                    "metadata": {
                        "name": self.name, "namespace": self.namespace,
                        "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": self.name, "uid": self.metadata["uid"]}],
                    },
                    "data": {".dockerconfigjson": base64.b64encode(json.dumps(self.credentials).encode()).decode()},
                }))
            deadline = time.monotonic() + timeout_seconds(self.timeout)
            wait_for_object(
                lambda: self.kubectl.get("pod", self.name, self.namespace, timeout=max(1.0, deadline - time.monotonic())),
                self._ready, deadline, f"compiler {self.namespace}/{self.name}",
            )
            self.run(["sh", "-ec", 'mkdir -p "$1"; rm -rf "$1/exports"; mkdir "$1/exports"', "prepare", self.execution_root])
            self.run(["sh", "-ec", 'mkdir -p "$1"; rm -rf "$1/output"; mkdir "$1/output"', "prepare", self.root], container=self.artifact_container)
            return self
        except BaseException as error:
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

    def export_application(self, component: str, destination: str, arguments: dict[str, str], parts: list[str] | None = None) -> None:
        """Compile selected first-party parts through the image build entrypoint and deliver their export."""
        output = self.execution_root + "/exports/" + component
        environment = {key: value for key, value in arguments.items() if key in {
            "GOPROXY", "GOSUMDB", "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_CARGO_REGISTRY",
            "CARGO_NET_GIT_FETCH_WITH_CLI", "FORETOKEN_MODEL_SERVER_BINARY",
        }}
        environment.update(
            CARGO_TARGET_DIR=self.execution_root + "/work/" + component + "/target",
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
            "--workspace", self.execution_root + "/work/" + component + "/source",
            "--versions", self.execution_root + "/inputs.json",
        ]
        defaults = ("go",) if component == "control-plane" else ("rust",) if component == "frontend" else ("rust", "python")
        selected = [part for part in (parts if parts is not None else defaults) if part != "engine"]
        if not selected:
            self.run(["mkdir", "-p", output])
        else:
            self.run([*command, "--parts", *selected], container=component)
        self._deliver_output(output, destination)

    def export_engine(self, destination: str, arguments: dict[str, str], *, cache_id: str, build_native: bool) -> None:
        """Compile engine files in the exact runtime's compiler image without changing its installed packages."""
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
        exported = destination + "-engine"
        self._deliver_output(output, exported)
        self.run(["sh", "-ec", 'cp -R "$1/engine" "$2/"; cp "$1/engine-environment.json" "$2/"', "assemble", exported, destination], container=self.artifact_container)

    def command(self, args: list[str], *, container: str = "builder") -> list[str]:
        """Address only this operation's Pod for streamed inputs and compiler commands."""
        return self.kubectl.command(["exec", "-i", "-n", self.namespace, self.name, "-c", container, "--", *args])

    def run(self, args: list[str], *, container: str = "builder", capture: bool = False, input_text: str | None = None) -> str:
        """Run a compiler stage under the caller's cancellation scope with live diagnostics."""
        result = self.cancellation.run(self.command(args, container=container), text=True, capture_output=capture, input=input_text)
        if result.returncode:
            self.cancellation.check()
            detail = (result.stderr or result.stdout or "").strip()
            raise DeploymentError(f"compiler {self.namespace}/{self.name} ({container}) failed: {detail or result.returncode}")
        return result.stdout if capture else ""

    def retire(self) -> None:
        """Delete and await this Pod's UID, leaving shared caches and workspace volumes intact."""
        current = self.kubectl.get_if_exists("pod", self.name, self.namespace)
        if current is None:
            return
        metadata = current["metadata"]
        if self.metadata is not None:
            if metadata["uid"] != self.metadata["uid"]:
                return
        elif not any(owner.get("uid") == self.owner["uid"] for owner in metadata.get("ownerReferences", [])):
            return
        try:
            self.kubectl.delete_object(current, self.timeout, match_version=False)
        except DeploymentError:
            current = self.kubectl.get_if_exists("pod", self.name, self.namespace)
            if current is not None and current["metadata"]["uid"] == metadata["uid"]:
                raise
            return

    def _ready(self, pod: dict[str, Any]) -> bool:
        """Surface startup failures while container diagnostics remain available."""
        self.cancellation.check()
        status = pod.get("status", {})
        for field in ("initContainerStatuses", "containerStatuses"):
            for container in status.get(field, []):
                terminated = container.get("state", {}).get("terminated")
                if terminated is not None and (field == "containerStatuses" or terminated["exitCode"]):
                    raise DeploymentError(f"compiler {self.name}: {container['name']}: {terminated.get('message') or terminated['reason']}")
        if status.get("phase") in {"Failed", "Succeeded"}:
            raise DeploymentError(f"compiler {self.name} stopped before becoming ready: {status.get('message') or status['phase']}")
        return any(condition["type"] == "Ready" and condition["status"] == "True" for condition in status.get("conditions", []))

    def _deliver_output(self, source: str, destination: str) -> None:
        """Copy only completed exports to the artifact writer, keeping native caches private."""
        self.run(["sh", "-ec", 'chmod -R g+rX "$1"', "share", source])
        self.run([
            "sh", "-ec", 'rm -rf "$2"; mkdir -p "$2"; cp -R --preserve=mode,timestamps --no-preserve=ownership "$1/." "$2/"',
            "deliver", source, destination,
        ], container=self.artifact_container)

    def _pod(self, shared: str, isolated: bool) -> dict[str, Any]:
        """Project private execution, shared native caches and the existing artifact-storage boundary."""
        execution_volume = "execution" if self.execution_claim != self.claim else "artifacts"
        volumes = [
            {"name": "artifacts", "persistentVolumeClaim": {"claimName": self.claim}},
            {"name": "dependencies", "persistentVolumeClaim": {"claimName": shared}},
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
        containers = [{
            "name": name, "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", "trap 'exit 0' TERM INT; sleep infinity & wait"],
            "workingDir": "/home/foretoken",
            "env": [{"name": "HOME", "value": "/home/foretoken"}, {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": security, "volumeMounts": mounts,
            "terminationMessagePolicy": "FallbackToLogsOnError",
        } for name, image in {"builder": self.tools_image, **self.images}.items()]
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
            "command": ["sh", "-ec", prepare, "prepare", str(SOURCE_BUILD_UID), self.execution_root, self.shared_mount, "/home/foretoken"],
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
                "name": self.name, "namespace": self.namespace, "ownerReferences": [self.owner],
                "labels": {SOURCE_BUILD_POD_LABEL: "true", SOURCE_BUILD_BINDING_LABEL: self.binding},
            },
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "imagePullSecrets": [{"name": name} for name in (*self.pull_secrets, *((self.name,) if self.credentials else ()))],
                "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                    "matchFields": [{"key": "metadata.name", "operator": "In", "values": [self.node]}],
                }]}}},
                "tolerations": [{"key": key, "operator": "Exists", "effect": "NoSchedule"} for key in (
                    "node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master",
                )],
                "volumes": volumes, "initContainers": init, "containers": containers,
            },
        }
