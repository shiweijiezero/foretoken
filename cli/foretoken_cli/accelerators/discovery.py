# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stateful exporter discovery shared by accelerator adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from foretoken_cli.accelerators._exporter import (
    monitor_selects_service,
    object_name,
    owned_by_daemonset,
    pod_ready,
    service_selects_pods,
)
from foretoken_cli.kubernetes import Kubectl, resource_ref
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.observability import (
    PrometheusRef,
    prometheus_selects_service_monitor,
)


@dataclass(frozen=True)
class ExporterMonitor:
    """An exporter and the ServiceMonitor proven to scrape it."""

    daemonset: ResourceRef
    service_monitor: ResourceRef
    service_monitor_labels: tuple[tuple[str, str], ...]


class ExporterDiscovery:
    """Own the Kubernetes inventory used by one accelerator discovery stage."""

    def __init__(self, kubectl: Kubectl) -> None:
        self._kubectl = kubectl
        self._nodes: tuple[dict[str, Any], ...] | None = None
        self._daemonsets: tuple[dict[str, Any], ...] | None = None
        self._monitoring_resources: frozenset[str] | None = None
        self._service_monitors: tuple[dict[str, Any], ...] | None = None
        self._services: dict[str, tuple[dict[str, Any], ...]] = {}
        self._pods: dict[str, tuple[dict[str, Any], ...]] = {}

    @property
    def nodes(self) -> tuple[dict[str, Any], ...]:
        """Return the node snapshot shared by accelerator adapters."""
        if self._nodes is None:
            self._nodes = self._kubectl.list_all_resources(("node",))
        return self._nodes

    @property
    def daemonsets(self) -> tuple[dict[str, Any], ...]:
        """Return the DaemonSet snapshot shared by exporter adapters."""
        if self._daemonsets is None:
            self._daemonsets = self._kubectl.list_all_resources(("daemonset.apps",))
        return self._daemonsets

    def find_monitor(
        self,
        candidates: tuple[dict[str, Any], ...],
        node_names: set[str],
        exporter_name: str,
        node_description: str,
    ) -> ExporterMonitor | None:
        """Return the unique exporter that covers the selected device nodes."""
        compatible = tuple(
            daemonset
            for daemonset in candidates
            if self._daemonset_covers_nodes(daemonset, node_names)
        )
        if len(compatible) > 1:
            names = ", ".join(self._display_name(value) for value in compatible)
            raise DeploymentError(
                f"multiple ready {exporter_name} instances cover "
                f"{node_description}: {names}"
            )
        if compatible:
            return self._service_monitor(compatible[0])
        if candidates:
            names = ", ".join(self._display_name(value) for value in candidates)
            raise DeploymentError(
                f"an existing {exporter_name} does not cover {node_description}; "
                f"repair its lifecycle before installing Foretoken: {names}"
            )
        return None

    def require_prometheus_selection(
        self,
        prometheus: PrometheusRef,
        exporters: tuple[tuple[str, ExporterMonitor], ...],
    ) -> None:
        """Require a shared Prometheus to collect every selected exporter."""
        for exporter_name, exporter in exporters:
            monitor = exporter.service_monitor
            if prometheus_selects_service_monitor(
                self._kubectl,
                prometheus,
                monitor.namespace,
                exporter.service_monitor_labels,
            ):
                continue
            raise DeploymentError(
                f"Prometheus {prometheus.namespace}/{prometheus.name} does not select "
                f"{exporter_name} ServiceMonitor {monitor.namespace}/{monitor.name}; "
                "update the shared platform selectors before installing Foretoken"
            )

    def _daemonset_covers_nodes(
        self, daemonset: dict[str, Any], node_names: set[str]
    ) -> bool:
        """Return whether ready DaemonSet Pods cover every selected node."""
        metadata = daemonset.get("metadata") or {}
        namespace = str(metadata.get("namespace") or "")
        uid = str(metadata.get("uid") or "")
        selector_spec = (daemonset.get("spec") or {}).get("selector") or {}
        if not isinstance(selector_spec, dict):
            return False
        match_labels = selector_spec.get("matchLabels", {})
        if not namespace or not uid or not isinstance(match_labels, dict):
            return False
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in match_labels.items()
        ):
            return False
        selector = ",".join(
            f"{key}={value}" for key, value in sorted(match_labels.items())
        )
        pods = self._pods.get(selector)
        if pods is None:
            pods = self._kubectl.list_all_resources(("pod",), label_selector=selector)
            self._pods[selector] = pods
        ready_nodes = {
            str((pod.get("spec") or {}).get("nodeName") or "")
            for pod in pods
            if str((pod.get("metadata") or {}).get("namespace") or "") == namespace
            and owned_by_daemonset(pod, uid)
            and pod_ready(pod)
        }
        return node_names.issubset(ready_nodes)

    def _service_monitor(self, daemonset: dict[str, Any]) -> ExporterMonitor:
        """Return the ServiceMonitor proven to scrape one exporter."""
        daemonset_ref = resource_ref(daemonset)
        if self._monitoring_resources is None:
            self._monitoring_resources = frozenset(
                self._kubectl.api_resource_names("monitoring.coreos.com")
            )
        if (
            "servicemonitors.monitoring.coreos.com"
            not in self._monitoring_resources
        ):
            raise DeploymentError(
                f"exporter {daemonset_ref.namespace}/{daemonset_ref.name} has no "
                "Prometheus Operator API for its ServiceMonitor"
            )

        template = (daemonset.get("spec") or {}).get("template") or {}
        pod_labels = ((template.get("metadata") or {}).get("labels") or {})
        if not isinstance(pod_labels, dict):
            raise DeploymentError("exporter Pod labels are invalid")
        pod_ports = {
            value
            for container in (
                ((daemonset.get("spec") or {}).get("template") or {}).get("spec") or {}
            ).get("containers")
            or []
            if isinstance(container, dict)
            for port in container.get("ports") or []
            if isinstance(port, dict)
            for value in (port.get("name"), port.get("containerPort"))
            if isinstance(value, (str, int))
        }
        services = self._services.get(daemonset_ref.namespace)
        if services is None:
            services = self._kubectl.list_resources(
                ("service",), daemonset_ref.namespace
            )
            self._services[daemonset_ref.namespace] = services
        selected_services = tuple(
            service
            for service in services
            if service_selects_pods(service, pod_labels)
        )
        if self._service_monitors is None:
            self._service_monitors = self._kubectl.list_all_resources(
                ("servicemonitors.monitoring.coreos.com",)
            )
        monitors = {
            resource_ref(monitor): monitor
            for monitor in self._service_monitors
            if any(
                monitor_selects_service(monitor, service, pod_ports)
                for service in selected_services
            )
        }
        if len(monitors) != 1:
            raise DeploymentError(
                f"exporter {daemonset_ref.namespace}/{daemonset_ref.name} needs "
                "exactly one ServiceMonitor that selects its metrics Service"
            )
        service_monitor, monitor = next(iter(monitors.items()))
        labels = (monitor.get("metadata") or {}).get("labels") or {}
        if not isinstance(labels, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in labels.items()
        ):
            raise DeploymentError("exporter ServiceMonitor metadata is invalid")
        return ExporterMonitor(
            daemonset_ref,
            service_monitor,
            tuple(sorted(labels.items())),
        )

    @staticmethod
    def _display_name(value: dict[str, Any]) -> str:
        """Return a stable namespaced identity for an exporter object."""
        ref = resource_ref(value)
        return f"{ref.namespace}/{ref.display_name}"


class AcceleratorMetricsDiscovery(ABC):
    """Share device-node and exporter selection across accelerator adapters."""

    exporter_name: str
    node_description: str

    def __init__(self, exporters: ExporterDiscovery) -> None:
        self._exporters = exporters

    def accelerator_nodes(self) -> tuple[dict[str, Any], ...]:
        """Return nodes that advertise this adapter's accelerator resource."""
        return tuple(
            node for node in self._exporters.nodes if self.has_capacity(node)
        )

    def exporter_candidates(self) -> tuple[dict[str, Any], ...]:
        """Return DaemonSets recognized by this accelerator adapter."""
        return tuple(
            daemonset
            for daemonset in self._exporters.daemonsets
            if self.is_exporter(daemonset)
        )

    def find_monitor(self, node_names: set[str]) -> ExporterMonitor | None:
        """Return the unique exporter collection path for selected nodes."""
        return self._exporters.find_monitor(
            self.exporter_candidates(),
            node_names,
            self.exporter_name,
            self.node_description,
        )

    @abstractmethod
    def has_capacity(self, node: dict[str, Any]) -> bool:
        """Return whether one node advertises this accelerator."""

    @abstractmethod
    def is_exporter(self, daemonset: dict[str, Any]) -> bool:
        """Return whether one DaemonSet is this adapter's exporter."""
