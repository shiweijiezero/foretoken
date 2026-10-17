# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve an administrator-selected NFS cache for namespace-local volume aliases."""

from __future__ import annotations

import json
from typing import Any

from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError


def prepare_shared_cache(
    kubectl: Kubectl, reference: str, timeout: str
) -> dict[str, Any]:
    """Return the selected RWX NFS location for namespace installation, retaining its backing volume.

    Namespace Helm releases own their alias claims and volumes; the source's model
    data remains independent of either the source claim or a candidate's removal.
    """
    namespace, separator, name = reference.partition("/")
    if not separator or not namespace or not name or "/" in name:
        raise DeploymentError("--shared-cache requires SOURCE_NAMESPACE/PVC")
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
        attributes = {key.lower(): value for key, value in source.get("volumeAttributes", {}).items()}
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
        raise DeploymentError("--shared-cache requires an NFS or nfs.csi.k8s.io volume")
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
        "directoryOwner": claim["metadata"].get("annotations", {}).get("inference.foretoken.io/directory-owner", ""),
    }
