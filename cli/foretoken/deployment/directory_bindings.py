# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare administrator-authorized filesystem directories as namespace-local claims."""

from __future__ import annotations

import copy
import json
import posixpath
import subprocess
from pathlib import Path
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.working_directory import repository_data_directory, resolve_runtime_directory


def _source_claim(
    kubectl: Kubectl, reference: str, timeout: str
) -> dict[str, Any]:
    """Return the selected RWX NFS location for namespace installation, retaining its backing volume.

    Namespace Helm releases own their alias claims and volumes; the source's model
    data remains independent of either the source claim or a candidate's removal.
    """
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


def prepare_directory_bindings(
    kubectl: Kubectl, namespace: str, directories: dict[str, Any],
    root: Path, default_claim: str, timeout: str,
) -> dict[str, Any]:
    """Resolve namespace installation's authorized roots while retaining external model storage."""
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
            raise DeploymentError(f"Directory {directory} has more than one namespace claim binding")
        seen.add(directory)
        if entry.get("existingClaim"):
            claim = kubectl.get("pvc", name, namespace)
            if claim["metadata"].get("deletionTimestamp"):
                raise DeploymentError(f"Directory claim {namespace}/{name} is being deleted")
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
                from foretoken.deployment.manifest import RuntimeCacheManifest
                from foretoken.deployment.storage import _resolve_directory

                directory.mkdir(exist_ok=True)
                cache = RuntimeCacheManifest(name, namespace, str(directory), "ReadWriteMany", "Retain")
                path, hostnames, owner = _resolve_directory(
                    cache, root, context, kubectl.list_cluster_resources(("nodes",)),
                )
                entry["hostPath"], entry["hostnames"] = path, hostnames
                if owner is not None:
                    entry["directoryOwner"] = f"{owner[0]}:{owner[1]}"
            else:
                source = _mounted_directory(directory)
                if source is None:
                    if not automatic:
                        raise DeploymentError(
                            f"Cannot discover shared storage for {directory}; namespace values must select its NFS source or an existing claim"
                        )
                    print(f"No shared mount was found for {directory}; no directory claim was prepared", flush=True)
                    del prepared[name]
                    continue
                entry.update(source)
        entry.setdefault("capacity", "1Gi")
    return prepared
