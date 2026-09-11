# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Deploy prepared directories through the RuntimeCache controller's PVC bindings."""

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

_CACHE_LABEL = "inference.foretoken.io/runtime-cache"
_NAMESPACE_LABEL = "inference.foretoken.io/runtime-cache-namespace"
_MANAGER_ANNOTATION = "inference.foretoken.io/directory-volume-manager"
_MANAGER = "foretoken-deploy"


class DirectoryVolumes:
    """Own static PV preparation and cleanup for CLI deployment operations.

    The controller remains the single owner of PVC names, capacity and retention.
    The CLI supplies prepared filesystem locations and preserves their contents.
    """

    def __init__(self, kubectl: Kubectl) -> None:
        self.kubectl = kubectl

    def apply(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Apply user intent, then bind directory PVs to the controller-created claims."""
        caches = tuple(cache for cache in deployment.runtime_caches if cache.directory)
        if not caches:
            self.kubectl.apply(deployment.rendered)
            return
        documents = copy.deepcopy(deployment.objects)
        context = self.kubectl.current_context()
        nodes = self.kubectl.list_cluster_resources(["nodes"])
        if not nodes:
            raise DeploymentError("directory storage requires a Kubernetes node")
        locations = []
        for cache in caches:
            path, hostnames = _resolve_directory(cache, deployment.path, context, nodes)
            locations.append((cache, path, hostnames))
            for document in documents:
                metadata = document.get("metadata") or {}
                if (
                    document.get("kind"),
                    metadata.get("namespace"),
                    metadata.get("name"),
                ) == ("RuntimeCache", cache.namespace, cache.name):
                    document["spec"]["directory"] = path

        # Let admission and the controller resolve the PVC contract before creating a PV.
        self.kubectl.apply(yaml.safe_dump_all(documents, sort_keys=False))
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
            self._bind(cache, claim, path, hostnames, timeout)

    def _bind(
        self,
        cache: RuntimeCacheManifest,
        claim: dict[str, Any],
        path: str,
        hostnames: list[str],
        timeout: str,
    ) -> None:
        """Create a volume once or rebind its retained data to a recreated claim."""
        desired = _persistent_volume(cache, claim, path, hostnames)
        name = desired["metadata"]["name"]
        current = self.kubectl.get_if_exists("pv", name)
        if current is None:
            self.kubectl.run(["create", "-f", "-"], input_text=yaml.safe_dump(desired))
            return
        if not _owned_volume(current, cache):
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
            if cache.directory and cache.retention_policy == "Delete":
                selector = (
                    f"{_CACHE_LABEL}={cache.name},{_NAMESPACE_LABEL}={cache.namespace}"
                )
                for volume in self.kubectl.list_cluster_resources(
                    ["pv"], label_selector=selector
                ):
                    if _owned_volume(volume, cache):
                        volumes.append((cache, volume))
        self.kubectl.delete(deployment.rendered, timeout)
        for cache, previous in volumes:
            name = previous["metadata"]["name"]
            current = self.kubectl.get_if_exists("pv", name)
            if current is None:
                continue
            if current["metadata"]["uid"] != previous["metadata"][
                "uid"
            ] or not _owned_volume(current, cache):
                raise DeploymentError(f"PV/{name} changed during deletion")
            claim = current["spec"].get("claimRef") or {}
            if claim and self.kubectl.get_if_exists(
                "pvc", claim["name"], claim["namespace"]
            ):
                raise DeploymentError(f"PV/{name} still has a claim")
            self.kubectl.run(
                ["delete", "pv", name, "--wait=true", f"--timeout={timeout}"]
            )


def _resolve_directory(
    cache: RuntimeCacheManifest,
    root: Path,
    context: str,
    nodes: tuple[dict[str, Any], ...],
) -> tuple[str, list[str]]:
    """Resolve a declared path and the node hostname labels that expose its files."""
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
        return str(path), [_hostname(nodes[0])] if len(nodes) == 1 else []

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
        if not mount.get("RW"):
            raise DeploymentError(f"directory mount in {container} must be writable")
        mappings.setdefault(str(Path(mount["Destination"]) / relative), []).append(
            _hostname(node)
        )
    if not mappings:
        raise DeploymentError(
            f"{context} does not mount {directory}; add a bind mount when creating its nodes"
        )
    if len(mappings) != 1:
        raise DeploymentError("k3d nodes must use the same directory mount destination")
    return next(iter(mappings.items()))


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
    cache: RuntimeCacheManifest, claim: dict[str, Any], path: str, hostnames: list[str]
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
            "labels": {_CACHE_LABEL: cache.name, _NAMESPACE_LABEL: cache.namespace},
            "annotations": {_MANAGER_ANNOTATION: _MANAGER},
        },
        "spec": spec,
    }


def _owned_volume(volume: dict[str, Any], cache: RuntimeCacheManifest) -> bool:
    """Identify only PVs prepared for this namespace/cache by the deploy command."""
    metadata = volume.get("metadata") or {}
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    return (
        labels.get(_CACHE_LABEL) == cache.name
        and labels.get(_NAMESPACE_LABEL) == cache.namespace
        and annotations.get(_MANAGER_ANNOTATION) == _MANAGER
    )
