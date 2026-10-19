# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve administrator-authorized directories before installing platform defaults."""

from __future__ import annotations

import copy
import json
import os
import posixpath
import subprocess
from pathlib import Path
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.working_directory import (
    repository_data_directory,
    resolve_runtime_directory,
)


def _source_claim(
    kubectl: Kubectl, reference: str, timeout: str
) -> dict[str, Any]:
    """Resolve an RWX NFS source and retain its volume before Go creates namespace aliases."""
    namespace, separator, name = reference.partition("/")
    if not separator or not namespace or not name or "/" in name:
        raise DeploymentError("directory sourceClaim requires SOURCE_NAMESPACE/PVC")
    seconds = timeout_seconds(timeout)
    claim = kubectl.get("pvc", name, namespace, timeout=seconds)
    if claim["metadata"].get("deletionTimestamp"):
        raise DeploymentError(f"Shared cache PVC {reference} is being deleted")
    if "ReadWriteMany" not in claim["spec"]["accessModes"]:
        raise DeploymentError(f"Shared cache PVC {reference} requires ReadWriteMany")
    if claim.get("status", {}).get("phase") != "Bound":
        identity = claim["metadata"]["uid"]
        kubectl.run(
            [
                "wait", f"pvc/{name}", "--namespace", namespace,
                "--for=jsonpath={.status.phase}=Bound", f"--timeout={timeout}",
            ],
            timeout=seconds,
        )
        claim = kubectl.get("pvc", name, namespace, timeout=seconds)
        if claim["metadata"]["uid"] != identity or claim["metadata"].get("deletionTimestamp"):
            raise DeploymentError(f"Shared cache PVC {reference} changed while binding")

    volume = kubectl.get("pv", claim["spec"]["volumeName"], timeout=seconds)
    metadata, spec = volume["metadata"], volume["spec"]
    if metadata.get("deletionTimestamp"):
        raise DeploymentError(f"Shared cache PV {metadata['name']} is being deleted")
    binding = spec.get("claimRef", {})
    if any(binding.get(key) != value for key, value in {
        "namespace": namespace, "name": name, "uid": claim["metadata"]["uid"],
    }.items()):
        raise DeploymentError(f"PV {metadata['name']} is not bound to shared cache PVC {reference}")
    if "ReadWriteMany" not in spec["accessModes"]:
        raise DeploymentError(f"Shared cache PV {metadata['name']} requires ReadWriteMany")
    options = list(spec.get("mountOptions", []))
    if "nfs" in spec:
        source = spec["nfs"]
        server, path = source["server"], source["path"]
        read_only = source.get("readOnly", False)
    elif spec.get("csi", {}).get("driver") == "nfs.csi.k8s.io":
        source = spec["csi"]
        attributes = {}
        for key, value in source.get("volumeAttributes", {}).items():
            key = key.lower()
            if key in attributes and attributes[key] != value:
                raise DeploymentError(f"Shared cache PV {metadata['name']} has conflicting NFS CSI attribute {key}")
            attributes[key] = value
        server, path = attributes.get("server", ""), attributes.get("share", "")
        if not server or not path:
            raise DeploymentError(f"Shared cache PV {metadata['name']} requires NFS CSI server and share attributes")
        subdirectory = attributes.get("subdir", "")
        # NodePublishVolume substitutes only metadata supplied in volumeContext;
        # volumeHandle identifies the volume but never determines its mount path.
        for placeholder, key in (
            ("${pvc.metadata.namespace}", "csi.storage.k8s.io/pvc/namespace"),
            ("${pvc.metadata.name}", "csi.storage.k8s.io/pvc/name"),
            ("${pv.metadata.name}", "csi.storage.k8s.io/pv/name"),
        ):
            if key in attributes:
                subdirectory = subdirectory.replace(placeholder, attributes[key])
        if subdirectory:
            path = path.rstrip("/") + "/" + subdirectory
        if ".." in path.replace("\\", "/").split("/"):
            raise DeploymentError(f"Shared cache PV {metadata['name']} has an invalid NFS CSI path")
        if attributes.get("mountoptions"):
            options.append(attributes["mountoptions"])
        read_only = source.get("readOnly", False)
    else:
        raise DeploymentError("directory sourceClaim requires an NFS or nfs.csi.k8s.io volume")
    if not server or not path.startswith("/"):
        raise DeploymentError(f"Shared cache PV {metadata['name']} requires an NFS server and absolute path")
    if read_only or any(option.strip() == "ro" for entry in options for option in entry.split(",")):
        raise DeploymentError(f"Shared cache PV {metadata['name']} is read-only")

    # Preserve the backing directory before any namespace can consume an alias.
    # Native preconditions reject a rebound or concurrently changed source volume.
    if spec["persistentVolumeReclaimPolicy"] != "Retain":
        kubectl.run(
            [
                "patch", "pv", metadata["name"], "--type=json", "--patch",
                json.dumps([
                    {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
                    {"op": "test", "path": "/spec/claimRef/uid", "value": claim["metadata"]["uid"]},
                    {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Retain"},
                ]),
            ],
            timeout=seconds,
        )
    return {
        "server": server,
        "path": path,
        "capacity": claim["spec"]["resources"]["requests"]["storage"],
        "mountOptions": options,
        "nodeAffinity": copy.deepcopy(spec.get("nodeAffinity", {})),
        "directoryOwner": claim["metadata"].get("annotations", {}).get("inference.foretoken.io/directory-owner", ""),
    }


def _mounted_directory(directory: Path) -> dict[str, Any] | None:
    """Read an actual local NFS mount rather than inferring storage from a pathname."""
    try:
        mounted = subprocess.run(
            ["findmnt", "--json", "--nofsroot", "--target", str(directory if directory.is_dir() else directory.parent),
             "--output", "FSTYPE,SOURCE,TARGET,FSROOT,OPTIONS"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if mounted.returncode:
        raise DeploymentError(f"Cannot read the filesystem mounted at {directory}: {mounted.stderr.strip()}")
    (filesystem,) = json.loads(mounted.stdout)["filesystems"]
    if filesystem["fstype"] not in {"nfs", "nfs4"}:
        return None
    server, separator, export = filesystem["source"].rpartition(":/")
    if not separator:
        raise DeploymentError(f"Cannot read the NFS source for {directory}: {filesystem['source']}")
    relative = directory.relative_to(Path(filesystem["target"]))
    path = posixpath.join("/" + export, filesystem["fsroot"].lstrip("/"), relative.as_posix())
    options = [
        option for option in filesystem["options"].split(",")
        if option.startswith(("vers=", "proto=", "port=", "sec="))
    ]
    if "ro" in filesystem["options"].split(","):
        raise DeploymentError(f"Directory {directory} is mounted read-only")
    source: dict[str, Any] = {"server": server, "path": posixpath.normpath(path), "mountOptions": options}
    directory.mkdir(exist_ok=True)
    identity = directory.stat()
    if identity.st_uid:
        source["directoryOwner"] = f"{identity.st_uid}:{identity.st_gid}"
    return source


def _resolve_k3d_directory(
    directory: Path, context: str, nodes: tuple[dict[str, Any], ...],
) -> tuple[str, list[str], tuple[int, int] | None]:
    """Inspect local node binds and return their common path, scheduling names and directory owner."""
    if os.environ.get("DOCKER_CONTEXT") or not os.environ.get("DOCKER_HOST"):
        endpoint = json.loads(_docker(["context", "inspect"]))[0]["Endpoints"]["docker"]["Host"]
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
            raise DeploymentError(f"Docker container {container} does not belong to {context}")
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
                candidate for candidate in info["Mounts"]
                if destination.is_relative_to(Path(candidate["Destination"]))
            ),
            key=lambda candidate: len(Path(candidate["Destination"]).parts),
        )
        if visible_mount != mount:
            raise DeploymentError(f"directory mount in {container} is hidden by {visible_mount['Destination']}")
        if not mount.get("RW"):
            raise DeploymentError(f"directory mount in {container} must be writable")
        mappings.setdefault(str(destination), []).append(_hostname(node))
    if not mappings:
        raise DeploymentError(f"{context} does not mount {directory}; add a bind mount when creating its nodes")
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
        raise DeploymentError(f"node {node['metadata']['name']} has no kubernetes.io/hostname label")
    return hostname


def _docker(args: list[str]) -> str:
    """Read local Docker metadata without creating or building containers."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise DeploymentError("Docker is required to inspect k3d directory mounts") from error
    if result.returncode:
        raise DeploymentError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def prepare_directory_bindings(
    kubectl: Kubectl, directories: dict[str, Any],
    root: Path, default_claim: str, timeout: str,
) -> dict[str, Any]:
    """Resolve authorized roots once; Go validates borrowed claims in each target namespace."""
    prepared = copy.deepcopy(directories)
    automatic = not prepared
    if automatic:
        directory = repository_data_directory(root)
        if not directory.is_dir() and not (directory.parent / "cli/foretoken").is_dir():
            return {}
        prepared[default_claim] = {"directory": str(directory)}
    seen = set()
    for name, entry in tuple(prepared.items()):
        directory = resolve_runtime_directory(entry["directory"], root)
        entry["directory"] = str(directory)
        if directory in seen:
            raise DeploymentError(f"Directory {directory} has more than one claim binding")
        seen.add(directory)
        if entry.get("existingClaim"):
            continue
        if reference := entry.pop("sourceClaim", None):
            source = _source_claim(kubectl, reference, timeout)
            entry.pop("hostPath", None)
            entry.pop("hostnames", None)
            if not source["directoryOwner"]:
                source.pop("directoryOwner")
            entry.update(source)
        elif not entry.get("server") and not entry.get("hostPath"):
            context = kubectl.current_context()
            if context.startswith("k3d-"):
                directory.mkdir(exist_ok=True)
                path, hostnames, owner = _resolve_k3d_directory(
                    directory, context, kubectl.list_cluster_resources(("nodes",)),
                )
                entry["hostPath"], entry["hostnames"] = path, hostnames
                if owner is not None:
                    entry["directoryOwner"] = f"{owner[0]}:{owner[1]}"
            else:
                source = _mounted_directory(directory)
                if source is None:
                    if not automatic:
                        raise DeploymentError(
                            f"Cannot discover shared storage for {directory}; platform values must select its NFS source or an existing claim"
                        )
                    print(f"No shared mount was found for {directory}; no directory claim was prepared", flush=True)
                    del prepared[name]
                    continue
                entry.update(source)
        entry.setdefault("capacity", "1Gi")
    return prepared
