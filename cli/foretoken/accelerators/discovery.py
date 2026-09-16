# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stateful exporter discovery shared by accelerator adapters."""

from __future__ import annotations

import json
import re
import time
from math import ceil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from foretoken.accelerators._exporter import (
    monitor_service_endpoints,
    owned_by_daemonset,
    pod_ready,
    service_selects_pods,
)
from foretoken.kubernetes import Kubectl, resource_ref
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.observability import (
    PrometheusRef,
    prometheus_query,
    prometheus_selects_service_monitor,
)


_METRIC_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
)
_METRIC_LABEL = re.compile(r'(?:^|,)\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)="')


@dataclass(frozen=True)
class MetricRequirement:
    """One metric family and labels required from an exporter endpoint."""

    name: str
    labels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExporterEndpoint:
    """One exporter Service endpoint selected by a ServiceMonitor."""

    service: ResourceRef
    port: str
    target_label: str
    path: str
    scheme: str


@dataclass(frozen=True)
class ExporterMonitor:
    """A validated exporter endpoint and its selected ServiceMonitor."""

    daemonset: ResourceRef
    service_monitor: ResourceRef
    service_monitor_labels: tuple[tuple[str, str], ...]
    endpoint: ExporterEndpoint
    target_pods: frozenset[str]


class ExporterDiscovery:
    """Own the Kubernetes inventory used by one accelerator discovery stage."""

    def __init__(self, kubectl: Kubectl, request_timeout: str) -> None:
        self._kubectl = kubectl
        self._request_timeout = request_timeout
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
        metric_requirements: tuple[MetricRequirement, ...],
        repair_hint: str,
    ) -> ExporterMonitor | None:
        """Return the unique healthy exporter covering the selected device nodes."""
        compatible = tuple(
            (daemonset, target_pods)
            for daemonset in candidates
            if (
                target_pods := self._daemonset_pods_on_nodes(
                    daemonset, node_names
                )
            )
        )
        if len(compatible) > 1:
            names = ", ".join(
                self._display_name(value) for value, _ in compatible
            )
            raise DeploymentError(
                f"multiple ready {exporter_name} instances cover "
                f"{node_description}: {names}"
            )
        if compatible:
            daemonset, target_pods = compatible[0]
            monitor = self._service_monitor(daemonset, target_pods)
            self._require_metrics(
                exporter_name, monitor.endpoint, metric_requirements, repair_hint
            )
            return monitor
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

    def require_prometheus_targets(
        self,
        prometheus: PrometheusRef,
        exporters: tuple[tuple[str, ExporterMonitor], ...],
        *,
        timeout_seconds: float = 0,
    ) -> None:
        """Require every exporter target to report up in the selected Prometheus."""
        deadline = time.monotonic() + timeout_seconds
        pending = exporters
        while pending:
            failed: list[tuple[str, ExporterMonitor, str]] = []
            for exporter_name, exporter in pending:
                endpoint = exporter.endpoint
                expression = (
                    "up{namespace="
                    + json.dumps(endpoint.service.namespace)
                    + ",service="
                    + json.dumps(endpoint.service.name)
                    + ",endpoint="
                    + json.dumps(endpoint.target_label)
                    + "}"
                )
                request_timeout = self._request_timeout
                if timeout_seconds > 0:
                    remaining = max(1, ceil(deadline - time.monotonic()))
                    request_timeout = f"{remaining}s"
                results = prometheus_query(
                    self._kubectl,
                    prometheus,
                    expression,
                    request_timeout,
                )
                values_by_pod: dict[str, list[str]] = {
                    pod: [] for pod in exporter.target_pods
                }
                for item in results:
                    metric = item.get("metric")
                    value = item.get("value")
                    if (
                        not isinstance(metric, dict)
                        or not isinstance(value, list)
                        or len(value) != 2
                    ):
                        continue
                    pod = metric.get("pod")
                    if pod in values_by_pod:
                        values_by_pod[pod].append(str(value[1]))
                missing = tuple(
                    pod for pod, values in values_by_pod.items() if not values
                )
                down = tuple(
                    pod
                    for pod, values in values_by_pod.items()
                    if any(value != "1" for value in values)
                )
                if missing or down:
                    reasons = []
                    if missing:
                        reasons.append("missing targets for " + ", ".join(missing))
                    if down:
                        reasons.append("targets down for " + ", ".join(down))
                    failed.append(
                        (exporter_name, exporter, "; ".join(reasons))
                    )
            if not failed:
                return
            if time.monotonic() >= deadline:
                details = "; ".join(
                    f"{name} ServiceMonitor {item.service_monitor.namespace}/"
                    f"{item.service_monitor.name} {reason} for Service "
                    f"{item.endpoint.service.namespace}/{item.endpoint.service.name} "
                    f"endpoint {item.endpoint.target_label}"
                    for name, item, reason in failed
                )
                raise DeploymentError(
                    f"Prometheus {prometheus.namespace}/{prometheus.name} cannot "
                    f"collect the selected GPU exporter: {details}; inspect its "
                    "Targets page and the exporter logs"
                )
            pending = tuple((name, item) for name, item, _ in failed)
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def _require_metrics(
        self,
        exporter_name: str,
        endpoint: ExporterEndpoint,
        requirements: tuple[MetricRequirement, ...],
        repair_hint: str,
    ) -> None:
        """Require one selected Service endpoint to expose its metric contract."""
        proxy_path = (
            f"/api/v1/namespaces/{endpoint.service.namespace}/services/"
            f"{endpoint.scheme}:{endpoint.service.name}:{endpoint.port}/proxy"
            f"{endpoint.path}"
        )
        try:
            body = self._kubectl.get_raw(proxy_path, self._request_timeout)
        except DeploymentError as exc:
            raise DeploymentError(
                f"{exporter_name} is Pod-ready but its metrics endpoint "
                f"{endpoint.service.namespace}/{endpoint.service.name}"
                f"{endpoint.path} is unavailable through the Kubernetes Service "
                f"proxy; {repair_hint}"
            ) from exc

        samples: dict[str, list[frozenset[str]]] = {}
        for line in body.splitlines():
            match = _METRIC_SAMPLE.match(line)
            if match is None:
                continue
            labels = frozenset(
                label.group("name")
                for label in _METRIC_LABEL.finditer(match.group("labels") or "")
            )
            samples.setdefault(match.group("name"), []).append(labels)
        missing = tuple(
            requirement.name
            for requirement in requirements
            if not any(
                requirement.labels.issubset(labels)
                for labels in samples.get(requirement.name, [])
            )
        )
        if missing:
            raise DeploymentError(
                f"{exporter_name} is Pod-ready but Service "
                f"{endpoint.service.namespace}/{endpoint.service.name} endpoint "
                f"{endpoint.path} does not expose the required metric contract "
                f"({', '.join(missing)}); {repair_hint}"
            )

    def _daemonset_pods_on_nodes(
        self, daemonset: dict[str, Any], node_names: set[str]
    ) -> frozenset[str]:
        """Return ready exporter Pod names when they cover every selected node."""
        metadata = daemonset.get("metadata") or {}
        namespace = str(metadata.get("namespace") or "")
        uid = str(metadata.get("uid") or "")
        selector_spec = (daemonset.get("spec") or {}).get("selector") or {}
        if not isinstance(selector_spec, dict):
            return frozenset()
        match_labels = selector_spec.get("matchLabels", {})
        if not namespace or not uid or not isinstance(match_labels, dict):
            return frozenset()
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in match_labels.items()
        ):
            return frozenset()
        selector = ",".join(
            f"{key}={value}" for key, value in sorted(match_labels.items())
        )
        pods = self._pods.get(selector)
        if pods is None:
            pods = self._kubectl.list_all_resources(("pod",), label_selector=selector)
            self._pods[selector] = pods
        ready_pods = {
            str((pod.get("spec") or {}).get("nodeName") or ""): str(
                (pod.get("metadata") or {}).get("name") or ""
            )
            for pod in pods
            if str((pod.get("metadata") or {}).get("namespace") or "") == namespace
            and owned_by_daemonset(pod, uid)
            and pod_ready(pod)
        }
        if not node_names.issubset(ready_pods):
            return frozenset()
        return frozenset(
            ready_pods[node] for node in node_names if ready_pods[node]
        )

    def _service_monitor(
        self, daemonset: dict[str, Any], target_pods: frozenset[str]
    ) -> ExporterMonitor:
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
        matches = tuple(
            (resource_ref(monitor), monitor, resource_ref(service), endpoint)
            for monitor in self._service_monitors
            for service in selected_services
            for endpoint in monitor_service_endpoints(monitor, service, pod_ports)
        )
        if len(matches) != 1:
            raise DeploymentError(
                f"exporter {daemonset_ref.namespace}/{daemonset_ref.name} needs "
                "exactly one ServiceMonitor endpoint selecting one metrics Service"
            )
        service_monitor, monitor, service, selected_endpoint = matches[0]
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
            ExporterEndpoint(
                service,
                selected_endpoint.port,
                selected_endpoint.target_label,
                selected_endpoint.path,
                selected_endpoint.scheme,
            ),
            target_pods,
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
    metric_requirements: tuple[MetricRequirement, ...]
    metrics_repair_hint: str

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
            self.metric_requirements,
            self.metrics_repair_hint,
        )

    @abstractmethod
    def has_capacity(self, node: dict[str, Any]) -> bool:
        """Return whether one node advertises this accelerator."""

    @abstractmethod
    def is_exporter(self, daemonset: dict[str, Any]) -> bool:
        """Return whether one DaemonSet is this adapter's exporter."""
