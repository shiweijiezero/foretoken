# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MetaX metric discovery lifecycle for platform installation."""

from __future__ import annotations

from typing import Any

from foretoken.accelerators._exporter import object_name
from foretoken.accelerators.config import METAX_GPU_RESOURCES
from foretoken.accelerators.discovery import (
    AcceleratorMetricsDiscovery,
    ExporterMonitor,
    MetricRequirement,
)
from foretoken.manifest import DeploymentError


class MetaXMetricsDiscovery(AcceleratorMetricsDiscovery):
    """Resolve MetaX nodes and their platform-provided mxExporter."""

    exporter_name = "mxExporter"
    node_description = "every MetaX GPU node"
    metric_requirements = (
        MetricRequirement("mx_gpu_usage", frozenset({"deviceId", "uuid"})),
        MetricRequirement(
            "mx_memory_usage", frozenset({"deviceId", "type", "uuid"})
        ),
    )
    metrics_repair_hint = (
        "configure the official exporter with GPU access, pod-resources and sysfs "
        "mounts, and the mx_gpu_usage and mx_memory_usage counters"
    )

    def resolve(self) -> ExporterMonitor | None:
        """Return the mxExporter collection path for allocatable MetaX GPUs."""
        gpu_nodes = {
            name
            for node in self.accelerator_nodes()
            for name in (object_name(node),)
            if name
        }
        if not gpu_nodes:
            return None

        monitor = self.find_monitor(gpu_nodes)
        if monitor is not None:
            return monitor
        raise DeploymentError(
            "MetaX GPU nodes require a platform-managed mxExporter and "
            "ServiceMonitor; install the official MetaX exporter before "
            "installing Foretoken"
        )

    def has_capacity(self, node: dict[str, Any]) -> bool:
        """Return whether Kubernetes advertises an allocatable MetaX GPU."""
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        for resource in METAX_GPU_RESOURCES:
            try:
                if int(str(allocatable.get(resource))) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def is_exporter(self, daemonset: dict[str, Any]) -> bool:
        """Recognize the official mxExporter DaemonSet identity."""
        metadata = daemonset.get("metadata") or {}
        labels = metadata.get("labels") or {}
        name = str(metadata.get("name") or "")
        return isinstance(labels, dict) and (
            name == "mx-exporter"
            or labels.get("app.kubernetes.io/name") == "mx-exporter"
            or labels.get("app") == "mx-exporter"
        )
