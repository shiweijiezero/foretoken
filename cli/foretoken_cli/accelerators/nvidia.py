# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""NVIDIA metric discovery lifecycle for platform installation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken_cli.accelerators._exporter import object_name
from foretoken_cli.accelerators.discovery import (
    AcceleratorMetricsDiscovery,
    ExporterMonitor,
)
from foretoken_cli.kubernetes import resource_ref
from foretoken_cli.manifest import DeploymentError, ResourceRef


@dataclass(frozen=True)
class NvidiaMetrics:
    """The selected exporter or safe placement for a managed release."""

    exporter: ExporterMonitor | None
    node_selector: tuple[str, str] | None


class NvidiaMetricsDiscovery(AcceleratorMetricsDiscovery):
    """Resolve NVIDIA nodes and the DCGM Exporter lifecycle for one install."""

    exporter_name = "DCGM Exporter"
    node_description = "every NVIDIA GPU node"

    def resolve(
        self, managed_daemonset: ResourceRef | None = None
    ) -> NvidiaMetrics | None:
        """Return NVIDIA metric collection or managed exporter placement."""
        candidate_nodes = tuple(
            node
            for node in self._exporters.nodes
            if not (node.get("spec") or {}).get("unschedulable")
        )
        blocked_gpu_nodes = tuple(
            name
            for node in candidate_nodes
            if self.has_capacity(node) and self.unsupported_taints(node)
            for name in (object_name(node),)
            if name
        )
        if blocked_gpu_nodes:
            raise DeploymentError(
                "NVIDIA GPU nodes have NoSchedule or NoExecute taints that the "
                "managed DCGM Exporter does not tolerate: "
                + ", ".join(sorted(blocked_gpu_nodes))
            )
        nodes = tuple(
            node for node in candidate_nodes if not self.unsupported_taints(node)
        )
        gpu_nodes = self.gpu_node_names(nodes)
        if not gpu_nodes:
            return None

        candidates = self.exporter_candidates()
        if managed_daemonset is not None:
            external = tuple(
                ref
                for value in candidates
                for ref in (resource_ref(value),)
                if ref != managed_daemonset
            )
            if external:
                names = ", ".join(
                    f"{ref.namespace}/{ref.display_name}" for ref in external
                )
                raise DeploymentError(
                    "a CLI-managed DCGM Exporter cannot coexist with another "
                    f"exporter: {names}"
                )
            return NvidiaMetrics(
                None, self.managed_node_selector(nodes, gpu_nodes)
            )

        monitor = self.find_monitor(set(gpu_nodes))
        return NvidiaMetrics(
            monitor,
            (
                None
                if monitor is not None
                else self.managed_node_selector(nodes, gpu_nodes)
            ),
        )

    def has_capacity(self, node: dict[str, Any]) -> bool:
        """Return whether Kubernetes advertises an allocatable NVIDIA GPU."""
        value = ((node.get("status") or {}).get("allocatable") or {}).get(
            "nvidia.com/gpu"
        )
        try:
            return int(str(value)) > 0
        except (TypeError, ValueError):
            return False

    def is_exporter(self, daemonset: dict[str, Any]) -> bool:
        """Recognize standalone and GPU Operator exporter identities."""
        metadata = daemonset.get("metadata") or {}
        labels = metadata.get("labels") or {}
        name = str(metadata.get("name") or "")
        return isinstance(labels, dict) and (
            name == "nvidia-dcgm-exporter"
            or labels.get("app.kubernetes.io/name") == "dcgm-exporter"
            or labels.get("app") in {"dcgm-exporter", "nvidia-dcgm-exporter"}
        )

    def managed_node_selector(
        self, nodes: tuple[dict[str, Any], ...], gpu_nodes: tuple[str, ...]
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
                if self.node_label(node, label) == "true"
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

    def unsupported_taints(self, node: dict[str, Any]) -> tuple[str, ...]:
        """Return scheduling taints not covered by managed chart defaults."""
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

    def gpu_node_names(
        self, nodes: tuple[dict[str, Any], ...]
    ) -> tuple[str, ...]:
        """Return stable names for nodes with NVIDIA GPU capacity."""
        return tuple(
            sorted(
                name
                for node in nodes
                if self.has_capacity(node)
                for name in (object_name(node),)
                if name
            )
        )

    @staticmethod
    def node_label(node: dict[str, Any], key: str) -> str:
        """Return one node label value when represented as a string."""
        labels = (node.get("metadata") or {}).get("labels") or {}
        if not isinstance(labels, dict):
            return ""
        return str(labels.get(key) or "")
