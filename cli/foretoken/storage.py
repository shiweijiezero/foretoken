# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Deploy prepared directories through the RuntimeCache controller's PVC bindings."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import (
    DeploymentError,
    ForetokenDeployment,
    RuntimeCacheManifest,
)

REMOTE_RUNTIME_CACHE_SIZE = "10Gi"


class DirectoryVolumes:
    """Own static and automatically provisioned RuntimeCache storage for deployments.

    The controller remains the single owner of PVC names, capacity and retention.
    The CLI supplies prepared filesystem locations and preserves their contents.
    """

    owner_annotation = (
        "inference.foretoken.io/directory-volume-manager",
        "foretoken-deploy",
    )
    directory_owner_annotation = "inference.foretoken.io/directory-owner"

    def __init__(self, kubectl: Kubectl) -> None:
        self.kubectl = kubectl

    def apply(
        self, deployment: ForetokenDeployment, timeout: str,
        *, submit: Callable[[str], None] | None = None,
    ) -> None:
        """Submit prepared intent, then bind directory PVs to the controller-created claims."""
        submit = submit or self.kubectl.apply
        caches = tuple(cache for cache in deployment.runtime_caches if cache.directory)
        if not caches:
            submit(deployment.rendered)
            return
        documents = copy.deepcopy(deployment.objects)
        context = self.kubectl.current_context()
        nodes = self.kubectl.list_cluster_resources(["nodes"])
        if not nodes:
            raise DeploymentError("directory-backed storage requires a Kubernetes node")
        locations = []
        for cache in caches:
            metadata_key = ("RuntimeCache", cache.namespace, cache.name)
            matching = [
                document
                for document in documents
                if (
                    document.get("kind"),
                    (document.get("metadata") or {}).get("namespace"),
                    (document.get("metadata") or {}).get("name"),
                ) == metadata_key
            ]
            if len(matching) != 1:
                raise DeploymentError(
                    f"could not find rendered RuntimeCache/{cache.name}"
                )
            spec = matching[0].setdefault("spec", {})
            annotations = matching[0]["metadata"].setdefault("annotations", {})
            path = Path(cache.directory)
            if not context.startswith("k3d-") and (
                not path.is_absolute() or ".." in path.parts
            ):
                annotations.pop(self.directory_owner_annotation, None)
                spec.pop("directory", None)
                spec.setdefault("initialSize", REMOTE_RUNTIME_CACHE_SIZE)
                schedulable_nodes = tuple(
                    node
                    for node in nodes
                    if not (node.get("spec") or {}).get("unschedulable")
                )
                if len(schedulable_nodes) == 1 and spec.get("accessMode") == "ReadWriteMany":
                    spec["accessMode"] = "ReadWriteOnce"
                    print(
                        f"RuntimeCache/{cache.name}: using ReadWriteOnce on the single-node cluster",
                        flush=True,
                    )
                print(
                    f"RuntimeCache/{cache.name}: using a dynamic PVC on the remote cluster",
                    flush=True,
                )
                continue
            consumer_nodes = nodes
            platform = None
            options = {}
            if not context.startswith("k3d-"):
                platforms = self.kubectl.list_all_resources(
                    ("deployment.apps",),
                    label_selector="app.kubernetes.io/name=foretoken-control-plane",
                )
                if len(platforms) == 1:
                    platform = platforms[0]
                    arguments = platform["spec"]["template"]["spec"]["containers"][0]["args"]
                    options = dict(
                        argument[2:].split("=", 1)
                        for argument in arguments
                        if argument.startswith("--") and "=" in argument
                    )
                    # Shared volumes retain their cluster-wide placement across redeployments.
                    # Only a local RWO directory needs proof of a single consumer node.
                    if cache.access_mode == "ReadWriteOnce":
                        consumer_nodes = self._consumer_nodes(deployment, cache, nodes, options)
            resolved, hostnames, directory_owner = _resolve_directory(
                cache, deployment.path, context, consumer_nodes
            )
            if not context.startswith("k3d-"):
                directory_owner = self.read_directory_owner(matching[0])
                if directory_owner is None:
                    directory_owner = self._remote_directory_owner(
                        resolved, consumer_nodes, timeout, platform, options
                    )
            if directory_owner is not None:
                uid, gid = directory_owner
                annotations[self.directory_owner_annotation] = f"{uid}:{gid}"
            else:
                annotations.pop(self.directory_owner_annotation, None)
            locations.append((cache, resolved, hostnames))
            spec["directory"] = resolved

        # Let admission and the controller resolve the PVC contract before creating a PV.
        submit(yaml.safe_dump_all(documents, sort_keys=False))
        for cache, path, hostnames in locations:
            self.kubectl.run(
                [
                    "wait",
                    f"runtimecache/{cache.name}",
                    "-n",
                    cache.namespace,
                    "--for=jsonpath={.status.claimName}",
                    f"--timeout={timeout}",
                ]
            )
            resource = self.kubectl.get("runtimecache", cache.name, cache.namespace)
            claim = self.kubectl.get(
                "pvc", resource["status"]["claimName"], cache.namespace
            )
            if not any(
                owner.get("uid") == resource["metadata"]["uid"]
                and owner.get("controller")
                for owner in claim["metadata"].get("ownerReferences", [])
            ):
                raise DeploymentError(
                    f"PVC/{claim['metadata']['name']} is not owned by RuntimeCache/{cache.name}"
                )
            self._bind(claim, path, hostnames, timeout)

    def _consumer_nodes(
        self,
        deployment: ForetokenDeployment,
        cache: RuntimeCacheManifest,
        nodes: tuple[dict[str, Any], ...],
        options: dict[str, str],
    ) -> tuple[dict[str, Any], ...]:
        """Resolve cache placement from installed runtime settings and namespace consumers."""
        caches = self.kubectl.list_resources(
            ("runtimecaches.inference.foretoken.io",), cache.namespace
        )
        if options.get("cache-claim") or any(
            current["metadata"]["name"] != cache.name for current in caches
        ):
            return nodes
        key = options.get("gpu-node-selector-key", "")
        value = options.get("gpu-node-selector-value", "")
        if bool(key) != bool(value):
            return nodes
        services = self.kubectl.list_resources(
            ("modelservices.inference.foretoken.io",), cache.namespace
        )
        # Keep existing intent as well as its replacement: old serving cohorts can
        # still use the claim while a deployment rolls onto a different node.
        services += tuple(
            document for document in deployment.objects
            if document.get("kind") == "ModelService"
            and document.get("apiVersion", "").startswith("inference.foretoken.io/")
            and document["metadata"]["namespace"] == cache.namespace
        )
        return _directory_nodes(nodes, services, {key: value} if key else {})

    def _remote_directory_owner(
        self,
        path: str,
        nodes: tuple[dict[str, Any], ...],
        timeout: str,
        platform: dict[str, Any] | None,
        options: dict[str, str],
    ) -> tuple[int, int] | None:
        """Read directory identity with the selected platform's tools before applying consumers."""
        if platform is None:
            raise DeploymentError(
                "automatic directory ownership discovery requires exactly one Foretoken control plane"
            )
        tools = json.loads(options.get("application-files", "{}"))
        if not tools.get("image"):
            raise DeploymentError(
                "automatic directory ownership discovery requires platform tools; run `foretoken install` first"
            )
        seconds = timeout_seconds(timeout)
        namespace = platform["metadata"]["namespace"]
        # The probe runs before a new workload namespace exists. Its credentials
        # must belong to the platform namespace, just like its tools image.
        pull_secrets = platform["spec"]["template"]["spec"].get("imagePullSecrets", [])
        owners = set()
        for node in nodes:
            print(f"Reading directory owner on {node['metadata']['name']}: {path}", flush=True)
            name = "foretoken-directory-owner-" + uuid.uuid4().hex[:12]
            job = {
                "apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": max(1, int(timeout_seconds(timeout))),
                    "ttlSecondsAfterFinished": 60,
                    "template": {"spec": {
                        "nodeName": node["metadata"]["name"],
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "imagePullSecrets": pull_secrets,
                        "securityContext": {
                            "runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [{
                            "name": "owner", "image": tools["image"],
                            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                            "terminationMessagePolicy": "FallbackToLogsOnError",
                            "command": ["python", "-I", "-B", "-c",
                                        "import os; s=os.stat('/directory'); print(f'{s.st_uid}:{s.st_gid}')"],
                            "securityContext": {
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{"name": "directory", "mountPath": "/directory", "readOnly": True}],
                        }],
                        "volumes": [{"name": "directory", "hostPath": {"path": path, "type": "Directory"}}],
                    }},
                },
            }
            created = json.loads(self.kubectl.run(
                ["create", "-f", "-", "-o", "json"], input_text=json.dumps(job), timeout=seconds
            ).stdout)["metadata"]
            try:
                deadline = time.monotonic() + timeout_seconds(timeout)
                while True:
                    current = json.loads(self.kubectl.run(
                        ["get", "job", name, "-n", namespace, "-o", "json"],
                        timeout=max(1, deadline - time.monotonic()),
                    ).stdout)
                    conditions = current.get("status", {}).get("conditions", [])
                    if any(c["type"] == "Complete" and c["status"] == "True" for c in conditions):
                        break
                    failed = next((c for c in conditions if c["type"] == "Failed" and c["status"] == "True"), None)
                    if failed or time.monotonic() >= deadline:
                        reason = failed.get("message", "Job failed") if failed else "timed out"
                        details = self.kubectl.run(
                            ["describe", "pods", "-n", namespace, "-l", "batch.kubernetes.io/controller-uid=" + created["uid"]],
                            timeout=seconds,
                        ).stdout
                        raise DeploymentError(f"cannot read owner of {path} on {node['metadata']['name']}: {reason}\n{details}")
                    time.sleep(1)
                output = self.kubectl.run(
                    ["logs", "job/" + name, "-n", namespace, "-c", "owner"],
                    timeout=max(1, deadline - time.monotonic()),
                ).stdout.strip()
                uid, gid = map(int, output.split(":"))
                owners.add((uid, gid))
            finally:
                current = json.loads(self.kubectl.run(
                    ["get", "job", name, "-n", namespace, "--ignore-not-found", "-o", "json"], timeout=seconds,
                ).stdout or "null")
                if current is not None and current["metadata"]["uid"] == created["uid"]:
                    try:
                        self.kubectl.run(
                            ["delete", "--raw", f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}", "-f", "-"],
                            input_text=json.dumps({
                                "apiVersion": "v1", "kind": "DeleteOptions",
                                "preconditions": {"uid": created["uid"]}, "propagationPolicy": "Foreground",
                            }), timeout=seconds,
                        )
                    except DeploymentError:
                        current = json.loads(self.kubectl.run(
                            ["get", "job", name, "-n", namespace, "--ignore-not-found", "-o", "json"], timeout=seconds,
                        ).stdout or "null")
                        if current is not None and current["metadata"]["uid"] == created["uid"]:
                            raise
                    self.kubectl.wait_deleted("jobs", created, timeout)
        if len(owners) != 1:
            raise DeploymentError(f"directory {path} has different owners across nodes")
        uid, gid = owners.pop()
        return (uid, gid) if uid != 0 else None

    def _bind(
        self,
        claim: dict[str, Any],
        path: str,
        hostnames: list[str],
        timeout: str,
    ) -> None:
        """Create a volume once or rebind its retained data to a recreated claim."""
        desired = _persistent_volume(claim, path, hostnames)
        key, owner = self.owner_annotation
        desired["metadata"]["annotations"] = {key: owner}
        name = desired["metadata"]["name"]
        current = self.kubectl.get_if_exists("pv", name)
        if current is None:
            self.kubectl.run(["create", "-f", "-"], input_text=yaml.safe_dump(desired))
            return
        if not self._owns_volume(current):
            raise DeploymentError(f"PV/{name} belongs to another deployment")
        spec = current["spec"]
        expected_claim = desired["spec"]["claimRef"]
        previous_claim = spec.get("claimRef") or {}
        if any(
            previous_claim.get(field) != expected_claim[field]
            for field in ("namespace", "name")
        ):
            raise DeploymentError(f"PV/{name} is assigned to another claim")
        fields = (
            "hostPath",
            "accessModes",
            "nodeAffinity",
            "persistentVolumeReclaimPolicy",
        )
        if spec.get("storageClassName", "") or any(
            spec.get(field) != desired["spec"].get(field) for field in fields
        ):
            raise DeploymentError(
                f"PV/{name} uses a different directory or node; choose a new cache name"
            )
        if current["metadata"].get("deletionTimestamp"):
            raise DeploymentError(f"PV/{name} is being deleted")
        old_uid = previous_claim.get("uid")
        if old_uid and old_uid != expected_claim["uid"]:
            # A new PVC with the same namespace/name proves the previous claim is gone.
            self.kubectl.run(
                [
                    "wait",
                    f"pv/{name}",
                    "--for=jsonpath={.status.phase}=Released",
                    f"--timeout={timeout}",
                ]
            )
            current = self.kubectl.get("pv", name)
            patch = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                },
                {"op": "test", "path": "/spec/claimRef/uid", "value": old_uid},
                {"op": "replace", "path": "/spec/claimRef", "value": expected_claim},
            ]
            self.kubectl.run(
                ["patch", "pv", name, "--type=json", "-p", json.dumps(patch)]
            )

    def delete(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Delete deployment resources and explicitly disposable PVs, never directory files."""
        volumes = []
        for cache in deployment.runtime_caches:
            if not cache.directory or cache.retention_policy != "Delete":
                continue
            resource = self.kubectl.get_if_exists(
                "runtimecache", cache.name, cache.namespace
            )
            if resource is None:
                continue
            claim_name = resource.get("status", {}).get("claimName")
            if not claim_name:
                continue
            claim = self.kubectl.get_if_exists("pvc", claim_name, cache.namespace)
            if claim is None or not claim["spec"].get("volumeName"):
                continue
            volume = self.kubectl.get_if_exists("pv", claim["spec"]["volumeName"])
            if volume is not None and self._owns_volume(volume):
                reference = volume["spec"].get("claimRef") or {}
                if reference.get("uid") != claim["metadata"]["uid"]:
                    raise DeploymentError(
                        f"PV/{volume['metadata']['name']} is assigned to another claim"
                    )
                volumes.append(volume)
        self.kubectl.delete(deployment.rendered, timeout)
        for previous in volumes:
            name = previous["metadata"]["name"]
            current = self.kubectl.get_if_exists("pv", name)
            if current is None:
                continue
            if current["metadata"]["uid"] != previous["metadata"][
                "uid"
            ] or not self._owns_volume(current):
                raise DeploymentError(f"PV/{name} changed during deletion")
            claim = current["spec"].get("claimRef") or {}
            if claim and self.kubectl.get_if_exists(
                "pvc", claim["name"], claim["namespace"]
            ):
                raise DeploymentError(f"PV/{name} still has a claim")
            self.kubectl.run(
                ["delete", "pv", name, "--wait=true", f"--timeout={timeout}"]
            )

    @classmethod
    def read_directory_owner(cls, cache: dict[str, Any]) -> tuple[int, int] | None:
        """Read the directory identity retained on a RuntimeCache or its PVC."""
        value = cache.get("metadata", {}).get("annotations", {}).get(
            cls.directory_owner_annotation
        )
        if value is None:
            return None
        try:
            uid, gid = map(int, value.split(":"))
            if not 0 < uid < 2**32 - 1 or not 0 <= gid < 2**32 - 1:
                raise ValueError
        except ValueError as exc:
            raise DeploymentError(f"invalid RuntimeCache directory owner: {value}") from exc
        return uid, gid

    def _owns_volume(self, volume: dict[str, Any]) -> bool:
        """Recognize this lifecycle's persistent marker before checking claim and path."""
        key, owner = self.owner_annotation
        return (volume.get("metadata", {}).get("annotations") or {}).get(key) == owner


def _directory_nodes(
    nodes: tuple[dict[str, Any], ...],
    services: tuple[dict[str, Any], ...],
    platform_selector: dict[str, str],
) -> tuple[dict[str, Any], ...]:
    """Union every cache consumer's inherited Pool placement without filtering node availability."""
    if not services:
        return nodes
    selected = set()
    for service in services:
        spec = service["spec"]
        for pool in spec.get("modelPools") or (spec,):
            selector = pool.get("nodeSelector")
            if selector is None:
                selector = spec.get("nodeSelector") or {}
            matches = tuple(
                node for node in nodes
                if all(
                    node["metadata"].get("labels", {}).get(key) == value
                    for constraint in (selector, platform_selector)
                    for key, value in constraint.items()
                )
            )
            # Unresolved consumers must not make another Pool appear single-node.
            # Admission and reconciliation own invalid or unsatisfiable selectors.
            if not matches:
                return nodes
            selected.update(node["metadata"]["name"] for node in matches)
    return tuple(node for node in nodes if node["metadata"]["name"] in selected)


def _resolve_directory(
    cache: RuntimeCacheManifest,
    root: Path,
    context: str,
    nodes: tuple[dict[str, Any], ...],
) -> tuple[str, list[str], tuple[int, int] | None]:
    """Resolve a node path and, for local binds, its non-root filesystem owner."""
    path = Path(cache.directory)
    if not context.startswith("k3d-"):
        if not path.is_absolute() or ".." in path.parts:
            raise DeploymentError(
                f"RuntimeCache/{cache.name}: set directory to an absolute node path"
            )
        if len(nodes) > 1 and cache.access_mode != "ReadWriteMany":
            raise DeploymentError(
                "a shared directory across nodes requires ReadWriteMany"
            )
        return str(path), [_hostname(nodes[0])] if len(nodes) == 1 else [], None

    directory = (path if path.is_absolute() else root / path).resolve()
    if not directory.is_dir():
        raise DeploymentError(
            f"RuntimeCache/{cache.name}: create {directory} before deploying"
        )
    if os.environ.get("DOCKER_CONTEXT") or not os.environ.get("DOCKER_HOST"):
        endpoint = json.loads(_docker(["context", "inspect"]))[0]["Endpoints"][
            "docker"
        ]["Host"]
    else:
        endpoint = os.environ["DOCKER_HOST"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise DeploymentError("k3d directory mapping needs a local Docker endpoint")

    # All eligible nodes must expose the same host tree at the same container path.
    mappings: dict[str, list[str]] = {}
    cluster = context.removeprefix("k3d-")
    for node in nodes:
        container = node["metadata"]["name"]
        if not container.startswith(f"k3d-{cluster}-"):
            continue
        info = json.loads(_docker(["inspect", container]))[0]
        if (info.get("Config", {}).get("Labels") or {}).get("k3d.cluster") != cluster:
            raise DeploymentError(
                f"Docker container {container} does not belong to {context}"
            )
        matches = []
        for mount in info.get("Mounts", []):
            if mount.get("Type") != "bind":
                continue
            source = Path(mount["Source"]).resolve()
            try:
                relative = directory.relative_to(source)
            except ValueError:
                continue
            matches.append((len(source.parts), mount, relative))
        if not matches:
            continue
        _, mount, relative = max(matches, key=lambda match: match[0])
        destination = Path(mount["Destination"]) / relative
        # A child mount can hide the chosen host tree inside the node, even when
        # the parent bind is writable. Bind only the directory actually exposed.
        visible_mount = max(
            (
                candidate
                for candidate in info["Mounts"]
                if destination.is_relative_to(Path(candidate["Destination"]))
            ),
            key=lambda candidate: len(Path(candidate["Destination"]).parts),
        )
        if visible_mount != mount:
            raise DeploymentError(
                f"directory mount in {container} is hidden by {visible_mount['Destination']}"
            )
        if not mount.get("RW"):
            raise DeploymentError(f"directory mount in {container} must be writable")
        mappings.setdefault(str(destination), []).append(_hostname(node))
    if not mappings:
        raise DeploymentError(
            f"{context} does not mount {directory}; add a bind mount when creating its nodes"
        )
    if len(mappings) != 1:
        raise DeploymentError("k3d nodes must use the same directory mount destination")
    destination, hostnames = next(iter(mappings.items()))
    status = directory.stat()
    owner = (status.st_uid, status.st_gid) if status.st_uid != 0 else None
    return destination, hostnames, owner


def _hostname(node: dict[str, Any]) -> str:
    """Read the scheduling label rather than assuming it equals the node name."""
    hostname = (node["metadata"].get("labels") or {}).get("kubernetes.io/hostname")
    if not hostname:
        raise DeploymentError(
            f"node {node['metadata']['name']} has no kubernetes.io/hostname label"
        )
    return hostname


def _docker(args: list[str]) -> str:
    """Inspect Docker mounts and preserve command diagnostics for deploy callers."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise DeploymentError(
            "Docker is required to inspect k3d directory mounts"
        ) from exc
    if result.returncode:
        raise DeploymentError(
            f"docker {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _persistent_volume(
    claim: dict[str, Any], path: str, hostnames: list[str]
) -> dict[str, Any]:
    """Match the controller's claim without redefining its name, capacity or access mode."""
    metadata, pvc_spec = claim["metadata"], claim["spec"]
    spec: dict[str, Any] = {
        "capacity": {"storage": pvc_spec["resources"]["requests"]["storage"]},
        "accessModes": pvc_spec["accessModes"],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "",
        "volumeMode": "Filesystem",
        "claimRef": {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
        },
        "hostPath": {"path": path, "type": "Directory"},
    }
    if hostnames:
        spec["nodeAffinity"] = {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": sorted(hostnames),
                            }
                        ]
                    }
                ]
            }
        }
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": pvc_spec["volumeName"],
        },
        "spec": spec,
    }
