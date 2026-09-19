# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MetaX exporter discovery and CLI-owned resource lifecycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from foretoken.accelerators._exporter import object_name
from foretoken.accelerators.config import METAX_GPU_RESOURCES
from foretoken.accelerators.discovery import (
    AcceleratorMetricsDiscovery,
    ExporterMonitor,
    MetricRequirement,
)
from foretoken.kubernetes import Kubectl, resource_ref
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.network_sources import platform_image_reference


@dataclass(frozen=True)
class MetaXMetrics:
    """Describe MetaX nodes and the exporter collection path for them."""

    node_names: tuple[str, ...]
    exporter: ExporterMonitor | None


class MetaXMetricsDiscovery(AcceleratorMetricsDiscovery):
    """Resolve MetaX nodes and an existing or CLI-managed exporter."""

    exporter_name = "mxExporter"
    node_description = "every MetaX GPU node"
    metric_requirements = (
        MetricRequirement("mx_gpu_usage", frozenset({"deviceId", "uuid"})),
        MetricRequirement("mx_memory_usage", frozenset({"deviceId", "type", "uuid"})),
    )
    metrics_repair_hint = (
        "configure the official exporter with GPU access, pod-resources and sysfs "
        "mounts, and the mx_gpu_usage and mx_memory_usage counters"
    )

    def resolve(
        self, managed_daemonset: ResourceRef | None = None
    ) -> MetaXMetrics | None:
        """Return collection or placement for installation; validate external exporters."""
        gpu_nodes = tuple(
            sorted(
                name
                for node in self.accelerator_nodes()
                for name in (object_name(node),)
                if name
            )
        )
        if not gpu_nodes:
            return None
        if managed_daemonset is not None:
            external = tuple(
                resource_ref(value)
                for value in self.exporter_candidates()
                if resource_ref(value) != managed_daemonset
            )
            if external:
                names = ", ".join(
                    f"{ref.namespace}/{ref.display_name}" for ref in external
                )
                raise DeploymentError(
                    "a CLI-managed mxExporter cannot coexist with another "
                    f"exporter: {names}"
                )
            # Reapply managed resources before checking readiness after a partial install.
            return MetaXMetrics(gpu_nodes, None)
        return MetaXMetrics(gpu_nodes, self.find_monitor(set(gpu_nodes)))

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


class MetaXExporterLifecycle:
    """Own installation and cleanup of the exporter in the packaged manifest."""

    def __init__(
        self,
        kubectl: Kubectl,
        management_label: tuple[str, str],
        image: str | None = None,
        image_registry: str | None = None,
    ) -> None:
        self._kubectl = kubectl
        self._management_label = management_label
        self._image = image
        self._image_registry = image_registry
        source = resources.files("foretoken.accelerators").joinpath("mx-exporter.yaml")
        documents = tuple(yaml.safe_load_all(source.read_text()))
        self._namespace = next(doc for doc in documents if doc["kind"] == "Namespace")
        self.namespace = self._namespace["metadata"]["name"]
        self._workloads = tuple(doc for doc in documents if doc["kind"] != "Namespace")
        for document in self._workloads:
            document["metadata"]["namespace"] = self.namespace
        daemonset = next(doc for doc in self._workloads if doc["kind"] == "DaemonSet")
        self.daemonset = resource_ref(daemonset)
        self._resources = tuple(resource_ref(doc) for doc in self._workloads)

    def _existing_resources(self) -> tuple[dict[str, Any], ...]:
        """Read exact identities, including before monitoring CRDs are installed."""
        monitors_available = "servicemonitors.monitoring.coreos.com" in (
            self._kubectl.api_resource_names("monitoring.coreos.com")
        )
        existing = []
        for ref in self._resources:
            if ref.kind == "ServiceMonitor" and not monitors_available:
                continue
            value = self._kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
            if value is not None:
                existing.append(value)
        return tuple(existing)

    def _owned(self, document: dict[str, Any]) -> bool:
        key, value = self._management_label
        return (document["metadata"].get("labels") or {}).get(key) == value

    def managed_resources(self) -> tuple[ResourceRef, ...]:
        """Return CLI-owned resources for planning install or uninstall."""
        return tuple(
            resource_ref(doc) for doc in self._existing_resources() if self._owned(doc)
        )

    def ensure_available(self) -> None:
        """Reject collisions before platform installation changes dependencies."""
        for document in self._existing_resources():
            if not self._owned(document):
                ref = resource_ref(document)
                raise DeploymentError(
                    f"cannot install MetaX mxExporter: {ref.namespace}/{ref.display_name} "
                    "already exists outside the Foretoken lifecycle"
                )

    def install(
        self,
        node_names: tuple[str, ...],
        service_monitor_labels: tuple[tuple[str, str], ...],
        timeout: str,
    ) -> None:
        """Apply the exporter to the selected nodes and wait for its rollout."""
        self.ensure_available()
        # Existing namespace metadata stays with its owner, and cleanup never deletes it.
        if not self._kubectl.exists("namespace", self.namespace):
            self._kubectl.apply(yaml.safe_dump(self._namespace))
        documents = deepcopy(self._workloads)
        for document in documents:
            labels = document["metadata"].setdefault("labels", {})
            if document["kind"] == "ServiceMonitor":
                labels.update(service_monitor_labels)
            labels.update([self._management_label])
            if document["kind"] == "DaemonSet":
                pod_spec = document["spec"]["template"]["spec"]
                pod_spec["affinity"] = {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchFields": [
                                        {
                                            "key": "metadata.name",
                                            "operator": "In",
                                            "values": [node_name],
                                        }
                                    ]
                                }
                                for node_name in node_names
                            ]
                        }
                    }
                }
                container = pod_spec["containers"][0]
                container["image"] = (
                    self._image if self._image is not None
                    else platform_image_reference(container["image"], self._image_registry)
                )
        self._kubectl.apply(yaml.safe_dump_all(documents, sort_keys=False))
        self._kubectl.rollout_status(self.daemonset, timeout)

    def uninstall(self, timeout: str) -> None:
        """Delete only CLI-owned exporter resources, retaining the namespace."""
        documents = tuple(doc for doc in self._existing_resources() if self._owned(doc))
        if documents:
            self._kubectl.delete(yaml.safe_dump_all(reversed(documents)), timeout)
