# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Discover shared RDMA allocations without changing the cluster's network owner."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError


_PLUGIN_IMAGE = "k8s-rdma-shared-dev-plugin"
_CONFIG_PATH = "/k8s-rdma-shared-dev-plugin"


@dataclass(frozen=True)
class RDMASelection:
    """One install-time allocation choice; None leaves explicit Helm values intact."""

    resource_name: str | None
    action: str
    detail: str
    managed: bool = False
    node_names: tuple[str, ...] = ()
    available: bool = False


def migrate_stored_rdma_values(values: dict[str, Any]) -> dict[str, Any]:
    """Move legacy allocation keys in installed Helm values to their platform owner.

    Current values files use the new schema; this only translates a previous release.
    Existing platform-level values take precedence over legacy P/D values.
    """
    migrated = deepcopy(values)
    pd = migrated.get("runtime", {}).get("vllm", {}).get("pd", {})
    for old, new in (
        ("rdmaResourceName", "resourceName"),
        ("rdmaResourceCount", "resourceCount"),
    ):
        if old in pd:
            value = pd.pop(old)
            migrated.setdefault("rdma", {}).setdefault(new, value)
    return migrated


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


def select_rdma(
    kubectl: Kubectl,
    nodes: tuple[dict[str, Any], ...],
    daemonsets: tuple[dict[str, Any], ...],
    values: tuple[dict[str, Any], ...],
    managed_release: tuple[str, str],
) -> RDMASelection:
    """Resolve a runtime allocation from explicit values or one advertised shared pool.

    Kubernetes allocatable values confirm availability; the plugin ConfigMap
    supplies resource identity. Neither NIC names nor capacity units are inferred.
    """
    resource: str | None = None
    managed: bool | None = None
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

    if managed and resource:
        raise DeploymentError(
            "rdma.managed and an external rdma.resourceName are mutually exclusive"
        )
    if not managed and resource is not None:
        return RDMASelection(
            None, "Configured" if resource else "Disabled", resource or "runtime RDMA disabled"
        )
    if not nodes:
        if managed:
            raise DeploymentError("managed RDMA requires selected GPU nodes; check GPU resources and the node selector")
        return RDMASelection(None, "Skip", "no selected GPU nodes")

    available: set[str] = set()
    external_plugins = []
    owned_plugin = False
    for daemonset in daemonsets:
        try:
            resources = shared_rdma_resources(kubectl, daemonset)
        except DeploymentError as exc:
            if managed:
                raise
            return RDMASelection(None, "Not available", str(exc))
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
        for node in nodes:
            allocatable = node.get("status", {}).get("allocatable", {})
            for name in resources:
                # Kubernetes serializes large allocations as quantities such as
                # 1k. Their coefficient is sufficient to determine availability.
                coefficient = re.split(
                    r"[a-zA-Z]", str(allocatable.get(name, 0)), maxsplit=1
                )[0]
                if Decimal(coefficient) > 0:
                    available.add(name)
    if managed is None:
        managed = not external_plugins
    if managed:
        if external_plugins:
            raise DeploymentError(
                "reuse the existing RDMA device plugin instead of enabling rdma.managed: "
                + ", ".join(external_plugins)
            )
        return RDMASelection(
            None,
            "Upgrade" if owned_plugin else "Install",
            "shared InfiniBand device plugin",
            managed=True,
            node_names=tuple(sorted(node["metadata"]["name"] for node in nodes)),
            available=bool(available),
        )
    if len(available) == 1:
        name = next(iter(available))
        return RDMASelection(name, "Reuse", name, available=True)
    if available:
        return RDMASelection(
            None, "Selection needed",
            "set rdma.resourceName to one of: " + ", ".join(sorted(available)),
        )
    if external_plugins:
        return RDMASelection(
            None, "Not available",
            "check the existing RDMA plugin's allocation on the selected GPU nodes: "
            + ", ".join(external_plugins),
        )
    return RDMASelection(
        None, "Unavailable",
        "no shared RDMA allocation on the selected GPU nodes; "
        "set rdma.managed=true to provision the device plugin on InfiniBand nodes",
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
