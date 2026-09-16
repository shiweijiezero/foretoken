# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MetaX metric discovery lifecycle for platform installation."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from foretoken.accelerators._exporter import object_name
from foretoken.accelerators.config import METAX_GPU_RESOURCES
from foretoken.accelerators.discovery import AcceleratorMetricsDiscovery, ExporterMonitor
from foretoken.manifest import DeploymentError


@dataclass(frozen=True)
class MetaXMetrics:
    """Describe MetaX nodes and the exporter collection path for them."""

    node_names: tuple[str, ...]
    exporter: ExporterMonitor | None


class MetaXMetricsDiscovery(AcceleratorMetricsDiscovery):
    """Resolve MetaX nodes and their platform-provided mxExporter."""

    exporter_name = "mxExporter"
    node_description = "every MetaX GPU node"

    def resolve(self) -> MetaXMetrics | None:
        """Return MetaX nodes and an existing qualified exporter, if present."""
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
        return MetaXMetrics(gpu_nodes, self.find_monitor(set(gpu_nodes)))

    @staticmethod
    def manifest(
        node_names: tuple[str, ...],
        namespace: str,
        image: str,
        service_monitor_labels: tuple[tuple[str, str], ...],
    ) -> str:
        """Render the packaged exporter manifest for the selected MetaX nodes."""
        try:
            source = resources.files("foretoken.accelerators").joinpath(
                "mx-exporter.yaml"
            ).read_text()
            documents = list(yaml.safe_load_all(source))
        except (OSError, yaml.YAMLError) as exc:
            raise DeploymentError("packaged MetaX exporter manifest is invalid") from exc
        if not all(isinstance(document, dict) for document in documents):
            raise DeploymentError("packaged MetaX exporter manifest is invalid")
        for document in documents:
            metadata = document.setdefault("metadata", {})
            if document.get("kind") != "Namespace":
                metadata["namespace"] = namespace
            labels = metadata.setdefault("labels", {})
            labels["foretoken.io/managed-by"] = "foretoken"
            labels["foretoken.io/component"] = "metax-exporter"
            if document.get("kind") == "ServiceMonitor":
                labels.update(dict(service_monitor_labels))
            if document.get("kind") == "DaemonSet":
                pod_spec = document["spec"]["template"]["spec"]
                pod_spec.pop("nodeSelector", None)
                pod_spec["affinity"] = {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [{
                                "matchExpressions": [{
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": list(node_names),
                                }]
                            }]
                        }
                    }
                }
                for container in pod_spec.get("containers", []):
                    if container.get("name") == "mx-exporter":
                        container["image"] = image
        return yaml.safe_dump_all(documents, sort_keys=False)

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
