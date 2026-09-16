# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stateless Kubernetes matching for accelerator metric exporters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken.observability import matches_label_selector


@dataclass(frozen=True)
class ServiceMonitorEndpoint:
    """One ServiceMonitor endpoint addressable through its selected Service."""

    port: str
    target_label: str
    path: str
    scheme: str


def object_name(value: dict[str, Any]) -> str:
    """Return one Kubernetes object's name when present."""
    return str((value.get("metadata") or {}).get("name") or "")


def service_selects_pods(
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


def monitor_service_endpoints(
    monitor: dict[str, Any], service: dict[str, Any], pod_ports: set[str | int]
) -> tuple[ServiceMonitorEndpoint, ...]:
    """Return selected exporter endpoints addressable through one Service."""
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
        or not monitor_namespace_matches(
            spec.get("namespaceSelector"), monitor_namespace, service_namespace
        )
    ):
        return ()

    service_ports = tuple(
        port
        for port in (service.get("spec") or {}).get("ports") or []
        if isinstance(port, dict)
    )
    selected: list[ServiceMonitorEndpoint] = []
    for endpoint in spec.get("endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        endpoint_port = endpoint.get("port")
        target_port = endpoint.get("targetPort")
        service_port = next(
            (
                port
                for port in service_ports
                if endpoint_port is not None and port.get("name") == endpoint_port
            ),
            None,
        )
        if service_port is None and target_port in pod_ports:
            service_port = next(
                (
                    port
                    for port in service_ports
                    if port.get("targetPort") == target_port
                    or port.get("port") == target_port
                ),
                None,
            )
        if service_port is None:
            continue
        proxy_port = service_port.get("name") or service_port.get("port")
        target_label = endpoint_port or target_port
        path = str(endpoint.get("path") or "/metrics")
        scheme = str(endpoint.get("scheme") or "http").lower()
        if (
            not isinstance(proxy_port, (str, int))
            or not isinstance(target_label, (str, int))
            or not path.startswith("/")
            or scheme not in {"http", "https"}
        ):
            continue
        selected.append(
            ServiceMonitorEndpoint(
                str(proxy_port), str(target_label), path, scheme
            )
        )
    return tuple(selected)


def monitor_namespace_matches(
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


def owned_by_daemonset(pod: dict[str, Any], uid: str) -> bool:
    """Return whether a Pod belongs to the selected DaemonSet."""
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    return any(
        isinstance(owner, dict)
        and owner.get("kind") == "DaemonSet"
        and owner.get("uid") == uid
        for owner in owners
    )


def pod_ready(pod: dict[str, Any]) -> bool:
    """Return whether a Pod reports its Ready condition true."""
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )
