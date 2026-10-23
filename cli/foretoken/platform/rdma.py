# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Discover shared RDMA allocations without changing the cluster's network owner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl

_PLUGIN_IMAGE = "k8s-rdma-shared-dev-plugin"
_CONFIG_PATH = "/k8s-rdma-shared-dev-plugin"


@dataclass(frozen=True)
class RDMASelection:
    """An install-time network owner and its advertised shared-allocation alternatives."""

    action: str
    detail: str
    managed: bool = False
    node_names: tuple[str, ...] = ()
    available: bool = False
    resource_names: tuple[str, ...] = ()


def select_rdma(
    kubectl: Kubectl,
    nodes: tuple[dict[str, Any], ...],
    daemonsets: tuple[dict[str, Any], ...],
    values: tuple[dict[str, Any], ...],
    managed_release: tuple[str, str],
) -> RDMASelection:
    """Resolve existing shared allocations or select managed device preparation.

    Kubernetes allocatable values confirm availability; the plugin ConfigMap
    supplies resource identity. Neither NIC names nor capacity units are inferred.
    """
    resource: str | None = None
    managed: bool | None = None
    bindings: list[dict[str, Any]] | None = None
    for document in values:
        rdma = document.get("rdma", {})
        if not isinstance(rdma, dict):
            raise DeploymentError("rdma must be a mapping")
        if "managed" in rdma:
            managed = rdma["managed"]
            if not isinstance(managed, bool):
                raise DeploymentError("rdma.managed must be a boolean")
        if "resourceName" in rdma:
            resource = rdma["resourceName"]
            if not isinstance(resource, str):
                raise DeploymentError("rdma.resourceName must be a string")
        if "bindings" in rdma:
            bindings = rdma["bindings"]
            if bindings is not None and not isinstance(bindings, list):
                raise DeploymentError("rdma.bindings must be an array or null")
            if bindings is not None and "managed" not in rdma:
                managed = False

    if bindings is not None:
        if managed:
            raise DeploymentError("rdma.bindings consume existing allocations and cannot enable rdma.managed")
        return RDMASelection("Configured" if bindings else "Disabled", "RDMA network bindings")
    if managed and resource:
        raise DeploymentError(
            "rdma.managed and an external rdma.resourceName are mutually exclusive"
        )
    if not managed and resource:
        return RDMASelection("Configured", resource)
    if not nodes:
        if managed:
            raise DeploymentError("managed RDMA requires selected GPU nodes; check GPU resources and the node selector")
        return RDMASelection("Skip", "no selected GPU nodes")

    available: set[str] = set()
    external_plugins = []
    owned_plugin = False
    for daemonset in daemonsets:
        try:
            resources = shared_rdma_resources(kubectl, daemonset)
        except DeploymentError as exc:
            if managed:
                raise
            return RDMASelection("Not available", str(exc))
        if resources is None:
            continue
        metadata = daemonset["metadata"]
        annotations = metadata.get("annotations", {})
        if (
            annotations.get("meta.helm.sh/release-name"),
            annotations.get("meta.helm.sh/release-namespace"),
        ) != managed_release:
            external_plugins.append(f"{metadata['namespace']}/{metadata['name']}")
        else:
            owned_plugin = True
            if managed is False:
                continue
        for name in resources:
            if nodes_with_rdma_allocation(nodes, name):
                available.add(name)
    network_allocators = _external_network_allocators(kubectl, nodes)
    if managed is None:
        managed = not external_plugins and not network_allocators
    if managed:
        if external_plugins or network_allocators:
            raise DeploymentError(
                "reuse the existing network allocator instead of enabling rdma.managed: "
                + ", ".join((*external_plugins, *network_allocators))
            )
        return RDMASelection(
            "Upgrade" if owned_plugin else "Install",
            "shared RDMA device plugin",
            managed=True,
            node_names=tuple(sorted(node["metadata"]["name"] for node in nodes)),
            available=bool(available),
        )
    if available:
        names = tuple(sorted(available))
        return RDMASelection(
            "Reuse", ", ".join(names),
            available=True, resource_names=names,
        )
    if external_plugins or network_allocators:
        return RDMASelection(
            "Not available",
            "existing network allocators have no reusable shared RDMA allocation on the selected GPU nodes: "
            + ", ".join((*external_plugins, *network_allocators)),
        )
    return RDMASelection(
        "Unavailable",
        "no shared RDMA allocation on the selected GPU nodes; "
        "set rdma.managed=true to provision the device plugin on RDMA-capable nodes",
    )


def _external_network_allocators(
    kubectl: Kubectl, nodes: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    """Preserve SR-IOV and DRA ownership before installing a blanket shared-HCA plugin."""
    owners = set()
    policies = "sriovnetworknodepolicies.sriovnetwork.openshift.io"
    if policies in kubectl.api_resource_names("sriovnetwork.openshift.io"):
        for policy in kubectl.list_all_resources((policies,)):
            spec = policy["spec"]
            if spec.get("isRdma") and any(
                all(node["metadata"].get("labels", {}).get(key) == value for key, value in (spec["nodeSelector"] or {}).items())
                for node in nodes
            ):
                metadata = policy["metadata"]
                owners.add(f"{metadata['namespace']}/{metadata['name']}")
    slices = "resourceslices.resource.k8s.io"
    if slices in kubectl.api_resource_names("resource.k8s.io"):
        node_names = {node["metadata"]["name"] for node in nodes}
        for resource_slice in kubectl.list_cluster_resources((slices,)):
            spec = resource_slice["spec"]
            if spec.get("nodeName") and spec["nodeName"] not in node_names:
                continue
            # Multus standard device attributes and DRANET's explicit RDMA bit
            # identify network ownership without interpreting arbitrary resource names.
            if any(
                "k8s.cni.cncf.io/deviceID" in device.get("attributes", {})
                or device.get("attributes", {}).get("dra.net/rdma", {}).get("bool") is True
                for device in spec.get("devices", [])
            ):
                owners.add(f"ResourceSlice/{resource_slice['metadata']['name']} ({spec['driver']})")
    return tuple(sorted(owners))


def nodes_with_rdma_allocation(
    nodes: tuple[dict[str, Any], ...], resource_name: str,
) -> tuple[dict[str, Any], ...]:
    """Return nodes that can consume the selected shared allocation for device and network preparation."""
    from kubernetes.utils.quantity import parse_quantity

    return tuple(
        node for node in nodes
        if parse_quantity(node.get("status", {}).get("allocatable", {}).get(resource_name, "0")) > 0
    )


def require_unused_managed_rdma(
    kubectl: Kubectl, release: tuple[str, str]
) -> None:
    """Preserve a managed device allocation while nonterminal Pods still request it."""
    resources: set[str] = set()
    for daemonset in kubectl.list_resources(("daemonset.apps",), release[1]):
        annotations = daemonset["metadata"].get("annotations", {})
        if (
            annotations.get("meta.helm.sh/release-name"),
            annotations.get("meta.helm.sh/release-namespace"),
        ) == release:
            resources.update(shared_rdma_resources(kubectl, daemonset) or ())
    if not resources:
        return
    users = []
    for pod in kubectl.list_all_resources(("pod",)):
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        spec = pod["spec"]
        containers = (*spec.get("initContainers", []), *spec.get("containers", []))
        if any(
            name in container.get("resources", {}).get("limits", {})
            for name in resources
            for container in containers
        ):
            metadata = pod["metadata"]
            users.append(f"{metadata['namespace']}/{metadata['name']}")
    if users:
        raise DeploymentError(
            "cannot remove the managed RDMA device plugin while Pods use its allocation: "
            + ", ".join(sorted(users))
        )


def shared_rdma_resources(
    kubectl: Kubectl, daemonset: dict[str, Any]
) -> frozenset[str] | None:
    """Read resources from an upstream shared-RDMA plugin's mounted configuration.

    None identifies a different device plugin. An unrecognized mount or argument
    is left to explicit platform configuration rather than guessed from resource names.
    """
    template = daemonset["spec"]["template"]
    pod = template["spec"]
    packaged = template.get("metadata", {}).get("labels", {}).get(
        "app.kubernetes.io/name"
    ) == "foretoken-rdma"
    for container in pod.get("containers", []):
        image = container.get("image", "").split("@", 1)[0].rsplit("/", 1)[-1]
        if image.split(":", 1)[0] != _PLUGIN_IMAGE and not (
            packaged and container.get("name") == "rdma-device-plugin"
        ):
            continue
        command = container.get("command", [])
        if command and command[0].rsplit("/", 1)[-1] != "k8s-rdma-shared-dp":
            return frozenset()
        arguments = (*command[1:], *container.get("args", []))
        if any(arg.split("=", 1)[0] in {"--config-file", "-config-file"} for arg in arguments):
            return frozenset()
        mounts = {
            mount["name"]
            for mount in container.get("volumeMounts", [])
            if mount.get("mountPath", "").rstrip("/") == _CONFIG_PATH
            and not mount.get("subPath")
        }
        for volume in pod.get("volumes", []):
            if volume["name"] not in mounts or "configMap" not in volume:
                continue
            source = volume["configMap"]
            key = "config.json"
            if "items" in source:
                keys = [item["key"] for item in source["items"] if item["path"] == key]
                if len(keys) != 1:
                    return frozenset()
                key = keys[0]
            configmap = kubectl.get(
                "configmap", source["name"], daemonset["metadata"]["namespace"]
            )
            try:
                config = json.loads(configmap["data"][key])
                return frozenset(
                    f"{entry.get('resourcePrefix', 'rdma')}/{entry['resourceName']}"
                    for entry in config["configList"]
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise DeploymentError(
                    f"cannot read RDMA resource configuration from "
                    f"{daemonset['metadata']['namespace']}/{source['name']}"
                ) from exc
        return frozenset()
    return None
