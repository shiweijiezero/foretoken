# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MetaX GPU and mxExporter discovery for platform installation."""

from __future__ import annotations

from typing import Any

from foretoken_cli.accelerators._exporter import (
    ExporterMonitor,
    daemonset_covers_nodes,
    exporter_service_monitor,
    object_name,
    resource_ref,
)
from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError

def discover_metax_metrics(kubectl: Kubectl) -> ExporterMonitor | None:
    """Return one ready mxExporter for clusters with allocatable MetaX GPUs."""
    gpu_nodes = {
        name
        for node in kubectl.list_all_resources(("node",))
        if _positive_metax_capacity(node)
        for name in (object_name(node),)
        if name
    }
    if not gpu_nodes:
        return None

    candidates = tuple(
        daemonset
        for daemonset in kubectl.list_all_resources(("daemonset.apps",))
        if _is_mx_exporter(daemonset)
    )
    compatible = tuple(
        (daemonset, ref)
        for daemonset in candidates
        if daemonset_covers_nodes(kubectl, daemonset, gpu_nodes)
        for ref in (resource_ref(daemonset),)
        if ref is not None
    )
    if len(compatible) > 1:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}" for _, ref in compatible
        )
        raise DeploymentError(
            f"multiple ready mxExporters cover MetaX GPU nodes: {names}"
        )
    if compatible:
        daemonset, daemonset_ref = compatible[0]
        return exporter_service_monitor(kubectl, daemonset, daemonset_ref)
    if candidates:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}"
            for value in candidates
            for ref in (resource_ref(value),)
            if ref is not None
        )
        raise DeploymentError(
            "an existing mxExporter does not cover every MetaX GPU node; "
            f"repair its platform lifecycle before installing Foretoken: {names}"
        )
    raise DeploymentError(
        "MetaX GPU nodes require a platform-managed mxExporter and ServiceMonitor; "
        "install the official MetaX exporter before installing Foretoken"
    )


def _positive_metax_capacity(node: dict[str, Any]) -> bool:
    """Return whether Kubernetes advertises an allocatable MetaX GPU."""
    allocatable = (node.get("status") or {}).get("allocatable") or {}
    for resource in ("metax-tech.com/gpu", "metax-tech.com/sgpu"):
        try:
            if int(str(allocatable.get(resource))) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _is_mx_exporter(daemonset: dict[str, Any]) -> bool:
    """Recognize the official mxExporter DaemonSet identity."""
    metadata = daemonset.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "")
    return isinstance(labels, dict) and (
        name == "mx-exporter"
        or labels.get("app.kubernetes.io/name") == "mx-exporter"
        or labels.get("app") == "mx-exporter"
    )
