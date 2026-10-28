# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""NVIDIA metric discovery lifecycle for platform installation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken.accelerators._exporter import object_name
from foretoken.accelerators.config import NVIDIA_GPU_RESOURCE
from foretoken.accelerators.discovery import (
    AcceleratorMetricsDiscovery,
    ExporterMonitor,
    MetricRequirement,
)
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import ResourceRef, resource_ref


@dataclass(frozen=True)
class NvidiaMetrics:
    """The selected exporter or safe placement for a managed release."""

    exporter: ExporterMonitor | None
    node_selector: tuple[str, str] | None
    visible_devices: str | None


class NvidiaMetricsDiscovery(AcceleratorMetricsDiscovery):
    """Resolve NVIDIA nodes and the DCGM Exporter lifecycle for one install."""

    exporter_name = "DCGM Exporter"
    node_description = "every NVIDIA GPU node"
    metric_requirements = (
        MetricRequirement("DCGM_FI_DEV_GPU_UTIL", frozenset({"UUID"})),
        MetricRequirement("DCGM_FI_DEV_FB_USED", frozenset({"UUID"})),
        MetricRequirement("DCGM_FI_DEV_FB_FREE", frozenset({"UUID"})),
    )
    metrics_repair_hint = (
        "enable device access and the Foretoken DCGM metric set on the exporter"
    )

    def resolve(
        self, managed_daemonset: ResourceRef | None = None
    ) -> NvidiaMetrics | None:
        """Return NVIDIA metric collection or managed exporter placement."""
        # Native DaemonSet tolerations can come from its owner's Helm values.
        # Discovery verifies actual Ready coverage instead of predicting placement.
        nodes = tuple(
            node
            for node in self._exporters.nodes
            if not (node.get("spec") or {}).get("unschedulable")
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
                None,
                self.managed_node_selector(nodes, gpu_nodes),
                self._visible_devices(gpu_nodes),
            )

        monitor = self.find_monitor(set(gpu_nodes))
        return NvidiaMetrics(
            monitor,
            (
                None
                if monitor is not None
                else self.managed_node_selector(nodes, gpu_nodes)
            ),
            self._visible_devices(gpu_nodes) if monitor is None else None,
        )

    def _visible_devices(self, gpu_nodes: tuple[str, ...]) -> str | None:
        """Reuse the covering NVIDIA plugin's literal visibility for managed DCGM placement."""
        # Nested nodes can lack driver mounts for GPUs outside the plugin's selection.
        plugins = []
        for daemonset in self._exporters.daemonsets:
            for container in daemonset["spec"]["template"]["spec"]["containers"]:
                command = container.get("command", [])
                image = container["image"].split("@", 1)[0].rsplit("/", 1)[-1]
                # Standard identities support mirrored images and entrypoint
                # wrappers. Other names must distinguish same-image sidecars.
                standard_plugin = (
                    object_name(daemonset) == "nvidia-device-plugin-daemonset"
                    and container["name"] == "nvidia-device-plugin-ctr"
                )
                is_plugin = standard_plugin or (
                    command[0].rsplit("/", 1)[-1] == "nvidia-device-plugin"
                    if command
                    else image.split(":", 1)[0] == "k8s-device-plugin"
                )
                if not is_plugin:
                    continue
                if self._exporters.daemonset_pods_on_nodes(daemonset, set(gpu_nodes)):
                    plugins.append(container)
        if len(plugins) != 1:
            return None
        for entry in plugins[0].get("env", []):
            if entry["name"] == "NVIDIA_VISIBLE_DEVICES":
                value = entry.get("value")
                # References use the plugin container's environment, not the exporter's.
                return value if value is not None and "$(" not in value else None
        return "all"

    def has_capacity(self, node: dict[str, Any]) -> bool:
        """Return whether Kubernetes advertises an allocatable NVIDIA GPU."""
        from kubernetes.utils.quantity import parse_quantity

        value = ((node.get("status") or {}).get("allocatable") or {}).get(
            NVIDIA_GPU_RESOURCE
        )
        try:
            return parse_quantity(value) > 0
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
