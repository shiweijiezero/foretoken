# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare user-owned directories as static volumes without copying their contents."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl
from foretoken.manifest import (
    DeploymentError,
    ForetokenDeployment,
    RuntimeCacheManifest,
)

# Static directories have no provisioned capacity or resize operation. Kubernetes
# still requires a positive binding request; this matches the RuntimeCache controller.
_DIRECTORY_BINDING_SIZE = "1Gi"
_CACHE_LABEL = "inference.foretoken.io/runtime-cache"
_NAMESPACE_LABEL = "inference.foretoken.io/runtime-cache-namespace"
_MANAGER_ANNOTATION = "inference.foretoken.io/directory-volume-manager"
_MANAGER = "foretoken-deploy"


def prepare_directory_storage(deployment: ForetokenDeployment, kubectl: Kubectl) -> str:
    """Create or reuse directory PVs and return resolved workload manifests for deploy.

    Relative paths describe directories mounted from the local Docker host into
    k3d. An absolute path on other clusters declares an already prepared node
    directory (shared on all nodes of a multi-node cluster). No data is uploaded.
    Existing volumes must retain the same declaration and namespace/cache identity.
    """
    caches = tuple(cache for cache in deployment.runtime_caches if cache.directory)
    if not caches:
        return deployment.rendered
    documents = copy.deepcopy(deployment.objects)
    context = kubectl.current_context()
    nodes = kubectl.list_cluster_resources(["nodes"])
    if not nodes:
        raise DeploymentError("directory storage requires at least one Kubernetes node")
    for cache in caches:
        path, hostnames = _resolve_directory(cache, deployment.path, context, nodes)
        volume = _persistent_volume(cache, path, hostnames)
        _ensure_volume(volume, kubectl)
        for document in documents:
            metadata = document.get("metadata") or {}
            if (
                document.get("kind"),
                metadata.get("namespace"),
                metadata.get("name"),
            ) == ("RuntimeCache", cache.namespace, cache.name):
                document["spec"]["directory"] = path
    return yaml.safe_dump_all(documents, sort_keys=False)


def _resolve_directory(
    cache: RuntimeCacheManifest,
    root: Path,
    context: str,
    nodes: tuple[dict[str, Any], ...],
) -> tuple[str, list[str]]:
    """Resolve a declared directory and the hostname labels where it is available."""
    if not context.startswith("k3d-"):
        path = Path(cache.directory)
        if not path.is_absolute() or ".." in path.parts:
            raise DeploymentError(
                f"RuntimeCache/{cache.name}: remote Kubernetes needs an absolute "
                "node directory; use a shared path on every node or dynamic PVC storage"
            )
        if len(nodes) > 1 and cache.access_mode != "ReadWriteMany":
            raise DeploymentError(
                "multi-node directory storage requires a shared directory and ReadWriteMany"
            )
        if len(nodes) == 1:
            return str(path), [_hostname(nodes[0])]
        # This is an explicit operator declaration, not inferred filesystem sharing.
        return str(path), []

    path = Path(cache.directory)
    directory = (path if path.is_absolute() else root / path).resolve()
    if not directory.is_dir():
        raise DeploymentError(
            f"RuntimeCache/{cache.name}: create directory {directory} before deploying"
        )
    _require_local_docker()
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
        if not mount.get("RW"):
            raise DeploymentError(
                f"cache directory is read-only in k3d node {container}"
            )
        destination = str(Path(mount["Destination"]) / relative)
        mappings.setdefault(destination, []).append(_hostname(node))
    if not mappings:
        raise DeploymentError(
            f"{context} does not mount {directory}; create its nodes with "
            "--volume HOST_DIRECTORY:NODE_DIRECTORY@all before deploying"
        )
    if len(mappings) != 1:
        raise DeploymentError(
            "k3d nodes must mount this directory at the same destination"
        )
    return next(iter(mappings.items()))


def _hostname(node: dict[str, Any]) -> str:
    """Read the scheduling label rather than assuming it equals the node object name."""
    hostname = (node["metadata"].get("labels") or {}).get("kubernetes.io/hostname")
    if not hostname:
        raise DeploymentError(
            f"node {node['metadata']['name']} has no kubernetes.io/hostname label"
        )
    return hostname


def _docker(args: list[str]) -> str:
    """Run Docker for local k3d mount inspection and preserve actionable diagnostics."""
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


def _require_local_docker() -> None:
    """Reject remote daemons: equal source strings do not identify client-local files."""
    if os.environ.get("DOCKER_CONTEXT") or not os.environ.get("DOCKER_HOST"):
        context = json.loads(_docker(["context", "inspect"]))[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
    else:
        endpoint = os.environ["DOCKER_HOST"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise DeploymentError(
            "relative directory deployment requires local Docker; remote Docker paths are not client paths"
        )


def _volume_name(cache: RuntimeCacheManifest) -> str:
    """Match the controller's namespace-qualified name without lossy truncation."""
    name = f"foretoken.{cache.namespace}.{cache.name}"
    if len(name) > 253:
        raise DeploymentError(
            "namespace and RuntimeCache name are too long for the directory PV name"
        )
    return name


def _persistent_volume(
    cache: RuntimeCacheManifest, path: str, hostnames: list[str]
) -> dict[str, Any]:
    """Describe a retained, prebound directory volume for the cache controller's PVC."""
    spec: dict[str, Any] = {
        "capacity": {"storage": _DIRECTORY_BINDING_SIZE},
        "accessModes": [cache.access_mode],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "",
        "volumeMode": "Filesystem",
        "claimRef": {"namespace": cache.namespace, "name": cache.name},
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
            "name": _volume_name(cache),
            "labels": {_CACHE_LABEL: cache.name, _NAMESPACE_LABEL: cache.namespace},
            "annotations": {_MANAGER_ANNOTATION: _MANAGER},
        },
        "spec": spec,
    }


def _owned_volume(volume: dict[str, Any], namespace: str, cache_name: str) -> bool:
    """Identify only directory PVs created by this deploy lifecycle."""
    metadata = volume.get("metadata") or {}
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    claim = (volume.get("spec") or {}).get("claimRef") or {}
    return (
        labels.get(_CACHE_LABEL) == cache_name
        and labels.get(_NAMESPACE_LABEL) == namespace
        and annotations.get(_MANAGER_ANNOTATION) == _MANAGER
        and claim.get("namespace") == namespace
        and claim.get("name") == cache_name
    )


def _ensure_volume(desired: dict[str, Any], kubectl: Kubectl) -> None:
    """Create once, or rebind an unchanged retained PV after its old claim was deleted."""
    name = desired["metadata"]["name"]
    current = kubectl.get_if_exists("persistentvolume", name)
    if current is None:
        # Create, not apply: a concurrently created volume must never be adopted implicitly.
        kubectl.run(["create", "-f", "-"], input_text=yaml.safe_dump(desired))
        return
    spec = current["spec"]
    claim = desired["spec"]["claimRef"]
    if not _owned_volume(current, claim["namespace"], claim["name"]):
        raise DeploymentError(
            f"PersistentVolume/{name} belongs to another storage lifecycle"
        )
    fields = (
        "hostPath",
        "accessModes",
        "nodeAffinity",
        "persistentVolumeReclaimPolicy",
    )
    if spec.get("storageClassName", "") != "" or any(
        spec.get(field) != desired["spec"].get(field) for field in fields
    ):
        raise DeploymentError(
            f"PersistentVolume/{name} has a different directory or placement; retain its data and use a new cache name"
        )
    if current["metadata"].get("deletionTimestamp"):
        raise DeploymentError(f"PersistentVolume/{name} is being deleted")
    if current.get("status", {}).get("phase") == "Released":
        if kubectl.get_if_exists(
            "persistentvolumeclaim", claim["name"], claim["namespace"]
        ):
            raise DeploymentError(
                f"PersistentVolume/{name} still has a claim; wait for deletion before redeploying"
            )
        # Clear only the stale claim UID, with a resourceVersion precondition.
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "replace", "path": "/spec/claimRef", "value": claim},
        ]
        kubectl.run(
            ["patch", "persistentvolume", name, "--type=json", "-p", json.dumps(patch)]
        )


def delete_directory_volumes(
    deployment: ForetokenDeployment, kubectl: Kubectl, timeout: str
) -> None:
    """Remove CLI-owned directory PV objects only for explicit Delete retention.

    Called after workload and claim deletion. The Retain PV policy never removes
    files; default Retain also keeps the PV object for a subsequent deployment.
    """
    for cache in deployment.runtime_caches:
        if not cache.directory or cache.retention_policy != "Delete":
            continue
        name = _volume_name(cache)
        current = kubectl.get_if_exists("persistentvolume", name)
        if current is None:
            continue
        if not _owned_volume(current, cache.namespace, cache.name):
            raise DeploymentError(
                f"PersistentVolume/{name} belongs to another storage lifecycle"
            )
        if kubectl.get_if_exists("persistentvolumeclaim", cache.name, cache.namespace):
            raise DeploymentError(
                f"PersistentVolume/{name} still has a claim; it was not deleted"
            )
        kubectl.run(
            ["delete", "persistentvolume", name, "--wait=true", f"--timeout={timeout}"]
        )
