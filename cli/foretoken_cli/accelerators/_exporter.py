# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared Kubernetes discovery for accelerator metric exporters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.observability import matches_label_selector


@dataclass(frozen=True)
class ExporterMonitor:
    """A reusable exporter and the ServiceMonitor proven to scrape it."""

    daemonset: ResourceRef
    service_monitor: ResourceRef
    service_monitor_labels: tuple[tuple[str, str], ...]


def object_name(value: dict[str, Any]) -> str:
    """Return one Kubernetes object's name when present."""
    return str((value.get("metadata") or {}).get("name") or "")


def resource_ref(value: dict[str, Any]) -> ResourceRef | None:
    """Return one namespaced resource identity when complete."""
    metadata = value.get("metadata") or {}
    kind = str(value.get("kind") or "")
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    if not kind or not name:
        return None
    return ResourceRef(kind, name, namespace)


def daemonset_covers_nodes(
    kubectl: Kubectl, daemonset: dict[str, Any], node_names: set[str]
) -> bool:
    """Return whether ready DaemonSet Pods cover every selected node."""
    metadata = daemonset.get("metadata") or {}
    namespace = str(metadata.get("namespace") or "")
    uid = str(metadata.get("uid") or "")
    daemonset_selector = (daemonset.get("spec") or {}).get("selector") or {}
    if not isinstance(daemonset_selector, dict):
        return False
    match_labels = daemonset_selector.get("matchLabels", {})
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
    ready_nodes = {
        str((pod.get("spec") or {}).get("nodeName") or "")
        for pod in kubectl.list_all_resources(("pod",), label_selector=selector)
        if str((pod.get("metadata") or {}).get("namespace") or "") == namespace
        and _owned_by(pod, uid)
        and _pod_ready(pod)
    }
    return node_names.issubset(ready_nodes)


def exporter_service_monitor(
    kubectl: Kubectl,
    daemonset: dict[str, Any],
    daemonset_ref: ResourceRef,
) -> ExporterMonitor:
    """Return the ServiceMonitor proven to scrape one reusable exporter."""
    resources = set(kubectl.api_resource_names("monitoring.coreos.com"))
    if "servicemonitors.monitoring.coreos.com" not in resources:
        raise DeploymentError(
            f"reused exporter {daemonset_ref.namespace}/{daemonset_ref.name} has no "
            "Prometheus Operator API for its ServiceMonitor"
        )
    pod_labels = (
        (((daemonset.get("spec") or {}).get("template") or {}).get("metadata") or {})
        .get("labels")
        or {}
    )
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
    services = tuple(
        service
        for service in kubectl.list_resources(("service",), daemonset_ref.namespace)
        if _service_selects_pods(service, pod_labels)
    )
    monitors = {
        ref: monitor
        for monitor in kubectl.list_all_resources(
            ("servicemonitors.monitoring.coreos.com",)
        )
        for ref in (resource_ref(monitor),)
        if ref is not None
        and any(
            _monitor_selects_service(monitor, service, pod_ports)
            for service in services
        )
    }
    if len(monitors) != 1:
        raise DeploymentError(
            f"reused exporter {daemonset_ref.namespace}/{daemonset_ref.name} needs "
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


def _service_selects_pods(
    service: dict[str, Any], pod_labels: dict[str, Any]
) -> bool:
    """Return whether a Service selects the exporter Pod template."""
    selector = (service.get("spec") or {}).get("selector") or {}
    return isinstance(selector, dict) and bool(selector) and all(
        isinstance(key, str)
        and isinstance(value, str)
        and pod_labels.get(key) == value
        for key, value in selector.items()
    )


def _monitor_selects_service(
    monitor: dict[str, Any], service: dict[str, Any], pod_ports: set[str | int]
) -> bool:
    """Return whether a ServiceMonitor selects one exporter Service and port."""
    monitor_metadata = monitor.get("metadata") or {}
    monitor_namespace = str(monitor_metadata.get("namespace") or "")
    service_metadata = service.get("metadata") or {}
    service_namespace = str(service_metadata.get("namespace") or "")
    service_labels = service_metadata.get("labels") or {}
    spec = monitor.get("spec") or {}
    if (
        not isinstance(service_labels, dict)
        or not isinstance(spec, dict)
        or not matches_label_selector(spec.get("selector"), service_labels)
        or not _monitor_namespace_matches(
            spec.get("namespaceSelector"), monitor_namespace, service_namespace
        )
    ):
        return False
    service_ports = {
        str(port.get("name") or "")
        for port in (service.get("spec") or {}).get("ports") or []
        if isinstance(port, dict) and port.get("name")
    }
    return any(
        isinstance(endpoint, dict)
        and (
            endpoint.get("port") in service_ports
            or endpoint.get("targetPort") in pod_ports
        )
        for endpoint in spec.get("endpoints") or []
    )


def _monitor_namespace_matches(
    selector: Any, monitor_namespace: str, service_namespace: str
) -> bool:
    """Evaluate the ServiceMonitor namespace-selection contract."""
    if selector is None:
        return monitor_namespace == service_namespace
    if not isinstance(selector, dict):
        return False
    if selector.get("any") is True:
        return True
    match_names = selector.get("matchNames") or []
    if not isinstance(match_names, list):
        return False
    if not match_names:
        return monitor_namespace == service_namespace
    return service_namespace in match_names


def _owned_by(pod: dict[str, Any], uid: str) -> bool:
    """Return whether a Pod belongs to the selected DaemonSet."""
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    return any(
        isinstance(owner, dict)
        and owner.get("kind") == "DaemonSet"
        and owner.get("uid") == uid
        for owner in owners
    )


def _pod_ready(pod: dict[str, Any]) -> bool:
    """Return whether a Pod reports its Ready condition true."""
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )
