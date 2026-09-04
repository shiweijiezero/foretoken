# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stateless Kubernetes matching for accelerator metric exporters."""

from __future__ import annotations

from typing import Any

from foretoken_cli.observability import matches_label_selector


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


def monitor_selects_service(
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
        or not monitor_namespace_matches(
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
