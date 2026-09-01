# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""NVIDIA GPU and DCGM Exporter discovery for platform installation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken_cli.accelerators._exporter import (
    ExporterMonitor,
    daemonset_covers_nodes,
    exporter_service_monitor,
    object_name,
    resource_ref,
)
from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError, ResourceRef

@dataclass(frozen=True)
class NvidiaMetrics:
    """The reusable exporter or safe placement for a managed release."""

    reused_exporter: ExporterMonitor | None
    node_selector: tuple[str, str] | None


def discover_nvidia_metrics(
    kubectl: Kubectl, managed_daemonset: ResourceRef | None = None
) -> NvidiaMetrics | None:
    """Resolve NVIDIA GPU nodes and a safe DCGM Exporter lifecycle."""
    candidate_nodes = tuple(
        node
        for node in kubectl.list_all_resources(("node",))
        if not (node.get("spec") or {}).get("unschedulable")
    )
    blocked_gpu_nodes = tuple(
        name
        for node in candidate_nodes
        if _positive_gpu_capacity(node) and _unsupported_taints(node)
        for name in (object_name(node),)
        if name
    )
    if blocked_gpu_nodes:
        raise DeploymentError(
            "NVIDIA GPU nodes have NoSchedule or NoExecute taints that the managed "
            "DCGM Exporter does not tolerate: " + ", ".join(sorted(blocked_gpu_nodes))
        )
    nodes = tuple(node for node in candidate_nodes if not _unsupported_taints(node))
    gpu_nodes = _gpu_node_names(nodes)
    if not gpu_nodes:
        return None

    candidates = tuple(
        daemonset
        for daemonset in kubectl.list_all_resources(("daemonset.apps",))
        if _is_dcgm_exporter(daemonset)
    )
    if managed_daemonset is not None:
        external = tuple(
            ref
            for value in candidates
            for ref in (resource_ref(value),)
            if ref is not None and ref != managed_daemonset
        )
        if external:
            names = ", ".join(
                f"{ref.namespace}/{ref.display_name}" for ref in external
            )
            raise DeploymentError(
                "a CLI-managed DCGM Exporter cannot coexist with another exporter: "
                + names
            )
        return NvidiaMetrics(None, _managed_node_selector(nodes, gpu_nodes))

    compatible = tuple(
        (daemonset, ref)
        for daemonset in candidates
        if daemonset_covers_nodes(kubectl, daemonset, set(gpu_nodes))
        for ref in (resource_ref(daemonset),)
        if ref is not None
    )
    if len(compatible) > 1:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}" for _, ref in compatible
        )
        raise DeploymentError(
            f"multiple ready DCGM Exporters cover NVIDIA GPU nodes: {names}"
        )
    if compatible:
        daemonset, daemonset_ref = compatible[0]
        return NvidiaMetrics(
            exporter_service_monitor(kubectl, daemonset, daemonset_ref),
            None,
        )
    if candidates:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}"
            for value in candidates
            for ref in (resource_ref(value),)
            if ref is not None
        )
        raise DeploymentError(
            "an existing DCGM Exporter does not cover every NVIDIA GPU node; "
            f"repair its lifecycle before installing Foretoken: {names}"
        )
    return NvidiaMetrics(None, _managed_node_selector(nodes, gpu_nodes))


def _managed_node_selector(
    nodes: tuple[dict[str, Any], ...], gpu_nodes: tuple[str, ...]
) -> tuple[str, str] | None:
    """Return a proven GPU-only node selector for a managed exporter."""
    schedulable_nodes = {
        name for node in nodes for name in (object_name(node),) if name
    }
    gpu_node_set = set(gpu_nodes)
    if schedulable_nodes == gpu_node_set:
        return None
    for label in (
        "nvidia.com/gpu.present",
        "feature.node.kubernetes.io/pci-10de.present",
    ):
        selected = {
            name
            for node in nodes
            if _node_label(node, label) == "true"
            for name in (object_name(node),)
            if name
        }
        if selected == gpu_node_set:
            return label, "true"
    raise DeploymentError(
        "NVIDIA GPU nodes need a GPU-only label before Foretoken can install "
        "DCGM Exporter; use nvidia.com/gpu.present=true or "
        "feature.node.kubernetes.io/pci-10de.present=true"
    )


def _unsupported_taints(node: dict[str, Any]) -> tuple[str, ...]:
    """Return scheduling taints not covered by the managed chart defaults."""
    tolerated = {
        ("node-role.kubernetes.io/control-plane", "NoSchedule"),
        ("node.kubernetes.io/disk-pressure", "NoSchedule"),
        ("node.kubernetes.io/memory-pressure", "NoSchedule"),
        ("node.kubernetes.io/not-ready", "NoExecute"),
        ("node.kubernetes.io/pid-pressure", "NoSchedule"),
        ("node.kubernetes.io/unreachable", "NoExecute"),
        ("node.kubernetes.io/unschedulable", "NoSchedule"),
    }
    taints = (node.get("spec") or {}).get("taints") or []
    return tuple(
        str(taint.get("key") or "")
        for taint in taints
        if isinstance(taint, dict)
        and taint.get("effect") in {"NoSchedule", "NoExecute"}
        and (str(taint.get("key") or ""), str(taint.get("effect") or ""))
        not in tolerated
    )


def _gpu_node_names(nodes: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    """Return stable names for nodes that advertise NVIDIA GPU capacity."""
    return tuple(
        sorted(
            name
            for node in nodes
            if _positive_gpu_capacity(node)
            for name in (object_name(node),)
            if name
        )
    )


def _positive_gpu_capacity(node: dict[str, Any]) -> bool:
    """Return whether Kubernetes advertises an allocatable NVIDIA GPU."""
    value = ((node.get("status") or {}).get("allocatable") or {}).get(
        "nvidia.com/gpu"
    )
    try:
        return int(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _node_label(node: dict[str, Any], key: str) -> str:
    """Return one node label value when represented as a string."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return ""
    return str(labels.get(key) or "")


def _is_dcgm_exporter(daemonset: dict[str, Any]) -> bool:
    """Recognize standalone and GPU Operator exporter identities."""
    metadata = daemonset.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "")
    return isinstance(labels, dict) and (
        name == "nvidia-dcgm-exporter"
        or labels.get("app.kubernetes.io/name") == "dcgm-exporter"
        or labels.get("app") in {"dcgm-exporter", "nvidia-dcgm-exporter"}
    )
