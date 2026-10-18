# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare owned artifact volumes before their first filesystem consumer."""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any

from foretoken.kubernetes import Kubectl, _wait_for_object, timeout_seconds
from foretoken.manifest import DeploymentError
from foretoken.node_images import SOURCE_BUILD_BINDING_LABEL, SOURCE_BUILD_POD_LABEL

if TYPE_CHECKING:
    from foretoken.command_execution import CommandExecution


def _bind_artifact_claim(
    kubectl: Kubectl,
    namespace: str,
    claim: dict[str, Any],
    node: str,
    tools_image: str,
    pull_secrets: tuple[str, ...],
    timeout: str,
    deadline: float,
    cancellation: CommandExecution,
) -> dict[str, Any]:
    """Let the native scheduler bind a claim without mounting it, then remove the owned Pod."""
    metadata = claim["metadata"]
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "activeDeadlineSeconds": max(1, math.ceil(deadline - time.monotonic())),
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 0,
        "imagePullSecrets": [{"name": secret} for secret in pull_secrets],
        "tolerations": [
            {"key": key, "operator": "Exists", "effect": "NoSchedule"}
            for key in (
                "node-role.kubernetes.io/control-plane",
                "node-role.kubernetes.io/master",
            )
        ],
        # Scheduling sees the claim; kubelet skips volumes unused by all containers.
        "volumes": [
            {"name": "artifacts", "persistentVolumeClaim": {"claimName": metadata["name"]}}
        ],
        "containers": [{
            "name": "binder",
            "image": tools_image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["sleep", "infinity"],
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": {
                "runAsUser": 1000, "runAsGroup": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        }],
    }
    if node:
        spec["affinity"] = {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                "matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]
            }]}
        }}
    cancellation.check()
    pod = json.loads(kubectl.run(
        ["create", "-f", "-", "-o", "json"],
        input_text=json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "generateName": "foretoken-artifact-bind-",
                "namespace": namespace,
                "labels": {
                    SOURCE_BUILD_POD_LABEL: "true",
                    SOURCE_BUILD_BINDING_LABEL: metadata["labels"][SOURCE_BUILD_BINDING_LABEL],
                },
                "ownerReferences": [{
                    "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "name": metadata["name"], "uid": metadata["uid"],
                }],
            },
            "spec": spec,
        }),
        timeout=max(1.0, deadline - time.monotonic()),
    ).stdout)
    identity = pod["metadata"]

    def remove_binder() -> None:
        """Delete only this binder UID, including when cancellation races normal cleanup."""
        current = kubectl.get_if_exists("pod", identity["name"], namespace, timeout=timeout_seconds(timeout))
        if current is None or current["metadata"]["uid"] != identity["uid"]:
            return
        try:
            kubectl.run(
                ["delete", "--raw", f"/api/v1/namespaces/{namespace}/pods/{identity['name']}", "-f", "-"],
                input_text=json.dumps({
                    "apiVersion": "v1", "kind": "DeleteOptions",
                    "preconditions": {"uid": identity["uid"]},
                }),
                timeout=timeout_seconds(timeout),
            )
        except DeploymentError:
            current = kubectl.get_if_exists("pod", identity["name"], namespace, timeout=timeout_seconds(timeout))
            if current is not None and current["metadata"]["uid"] == identity["uid"]:
                raise
            return
        kubectl.wait_deleted("pod", identity, timeout)

    def bound(value: dict[str, Any]) -> bool:
        """Reject a replaced or retiring claim rather than preparing another owner's volume."""
        cancellation.check()
        current = value["metadata"]
        if (
            current["uid"] != metadata["uid"]
            or current.get("deletionTimestamp")
            or any(current.get("labels", {}).get(key) != metadata["labels"][key] for key in (
                "inference.foretoken.io/source-build-cache", SOURCE_BUILD_BINDING_LABEL,
            ))
        ):
            raise DeploymentError(f"artifact PVC {namespace}/{metadata['name']} changed while binding")
        if value.get("status", {}).get("phase") == "Lost":
            raise DeploymentError(f"artifact PVC {namespace}/{metadata['name']} lost its volume")
        return value.get("status", {}).get("phase") == "Bound"

    error = None
    try:
        cancellation.register(remove_binder)
        return _wait_for_object(
            lambda: kubectl.get("pvc", metadata["name"], namespace, timeout=max(1.0, deadline - time.monotonic())),
            bound, deadline, f"artifact PVC {namespace}/{metadata['name']} to bind",
        )
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            cancellation.cleanup(remove_binder, error)
        finally:
            cancellation.unregister(remove_binder)


def prepare_artifact_volume(
    kubectl: Kubectl,
    namespace: str,
    claim: str,
    binding: str,
    node: str,
    tools_image: str,
    pull_secrets: tuple[str, ...],
    timeout: str,
    cancellation: CommandExecution,
) -> bool:
    """Prepare an owned artifact PVC and report whether its NFS mount lifecycle is isolated.

    Bind through the native scheduler if needed, without mounting the filesystem.
    Only explicit NFS sources receive nosharecache; other storage is unchanged.
    Existing isolated volumes are reusable, including while consumers are running.
    """
    cancellation.check()
    deadline = time.monotonic() + timeout_seconds(timeout)
    pvc = kubectl.get("pvc", claim, namespace, timeout=max(1.0, deadline - time.monotonic()))
    metadata = pvc["metadata"]
    labels = metadata.get("labels", {})
    if labels.get("inference.foretoken.io/source-build-cache") != "true" or labels.get(SOURCE_BUILD_BINDING_LABEL) != binding:
        raise DeploymentError(f"artifact PVC {namespace}/{claim} is not owned by source binding {binding}")
    if metadata.get("deletionTimestamp"):
        raise DeploymentError(f"artifact PVC {namespace}/{claim} is being deleted")
    if pvc.get("status", {}).get("phase") != "Bound":
        pvc = _bind_artifact_claim(
            kubectl, namespace, pvc, node, tools_image, pull_secrets,
            timeout, deadline, cancellation,
        )
    cancellation.check()
    volume = kubectl.get("pv", pvc["spec"]["volumeName"], timeout=max(1.0, deadline - time.monotonic()))
    spec = volume["spec"]
    reference = spec.get("claimRef", {})
    if any(reference.get(key) != value for key, value in {
        "name": claim, "namespace": namespace, "uid": metadata["uid"],
    }.items()):
        raise DeploymentError(f"PV {volume['metadata']['name']} is not bound to artifact PVC {namespace}/{claim} UID {metadata['uid']}")
    if "nfs" not in spec and spec.get("csi", {}).get("driver") != "nfs.csi.k8s.io":
        return False
    options = spec.get("mountOptions", [])
    if "nosharecache" in options and "sharecache" not in options:
        return True
    if not volume["metadata"].get("annotations", {}).get("pv.kubernetes.io/provisioned-by"):
        raise DeploymentError(
            f"cannot isolate artifact PVC {namespace}/{claim}: PV {volume['metadata']['name']} "
            "is not dynamically provisioned; its owner must set nosharecache and remove sharecache before reuse"
        )

    # Never change mount behavior beneath a real consumer, even if it is terminating.
    pods = json.loads(kubectl.run(
        ["get", "pods", "-n", namespace, "-o", "json"],
        timeout=max(1.0, deadline - time.monotonic()),
    ).stdout)["items"]
    for pod in pods:
        pod_spec = pod["spec"]
        volumes = {
            item["name"] for item in pod_spec.get("volumes", [])
            if item.get("persistentVolumeClaim", {}).get("claimName") == claim
        }
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for container in pod_spec.get(field, []):
                if any(mount["name"] in volumes for mount in container.get("volumeMounts", [])):
                    raise DeploymentError(
                        f"cannot isolate artifact PVC {namespace}/{claim}: "
                        f"Pod {namespace}/{pod['metadata']['name']} container {container['name']} mounts it; "
                        "finish that consumer before preparing artifact storage"
                    )
    cancellation.check()
    kubectl.run(
        ["patch", "pv", volume["metadata"]["name"], "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/uid", "value": volume["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": volume["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/claimRef/uid", "value": metadata["uid"]},
            {"op": "add", "path": "/spec/mountOptions", "value": [
                option for option in options if option not in {"sharecache", "nosharecache"}
            ] + ["nosharecache"]},
        ])],
        timeout=max(1.0, deadline - time.monotonic()),
    )
    return True
