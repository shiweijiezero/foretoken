# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read-only discovery of Prometheus instances compatible with Foretoken."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError

_PROMETHEUS_API_GROUP = "monitoring.coreos.com"
_REQUIRED_PROMETHEUS_RESOURCES = frozenset(
    {
        "prometheuses.monitoring.coreos.com",
        "servicemonitors.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
    }
)
_METRICS_SCRAPER_LABEL = "inference.foretoken.io/metrics-scraper"
_FORETOKEN_LABELS = {
    "app.kubernetes.io/name": "foretoken-control-plane",
    "app.kubernetes.io/instance": "foretoken",
    "app.kubernetes.io/managed-by": "Helm",
}


@dataclass(frozen=True)
class PrometheusRef:
    """A ready Prometheus instance and labels Foretoken must add to its resources."""

    name: str
    namespace: str
    additional_labels: tuple[tuple[str, str], ...]


def select_prometheus(
    kubectl: Kubectl, platform_namespace: str, requested: str | None
) -> PrometheusRef | None:
    """Return one compatible Prometheus for installation, or None when none exists."""
    requested_identity = _requested_identity(requested)
    resources = set(kubectl.api_resource_names(_PROMETHEUS_API_GROUP))
    if not _REQUIRED_PROMETHEUS_RESOURCES.issubset(resources):
        if requested_identity is not None:
            raise DeploymentError(
                "--prometheus requires Prometheus Operator APIs in the cluster"
            )
        return None

    prometheuses = kubectl.list_all_resources(
        ("prometheuses.monitoring.coreos.com",)
    )
    if not prometheuses:
        if requested_identity is not None:
            raise DeploymentError(
                f"requested Prometheus {requested} does not exist; available instances: none"
            )
        return None

    if requested_identity is not None:
        selected = tuple(
            value
            for value in prometheuses
            if _identity(value) == requested_identity
        )
        if not selected:
            available = _available_identities(prometheuses)
            raise DeploymentError(
                f"requested Prometheus {requested} does not exist; available "
                f"instances: {available or 'none'}"
            )
    else:
        selected = prometheuses

    namespace = kubectl.get_if_exists("namespace", platform_namespace) or {}
    namespace_labels = _namespace_labels(namespace, platform_namespace)
    compatible = tuple(
        ref
        for value in selected
        if _prometheus_namespace_is_trusted(kubectl, value)
        and (
            ref := _compatible_prometheus(
                value,
                platform_namespace,
                namespace_labels,
            )
        )
        is not None
    )
    if not compatible:
        if requested_identity is not None:
            raise DeploymentError(
                f"requested Prometheus {requested} is unavailable or cannot select "
                f"Foretoken ServiceMonitors and PrometheusRules in namespace "
                f"{platform_namespace}; verify its selectors and label its namespace "
                f"with {_METRICS_SCRAPER_LABEL}=true"
            )
        raise DeploymentError(
            "Prometheus instances exist, but none is available and can select "
            f"Foretoken ServiceMonitors and PrometheusRules in namespace "
            f"{platform_namespace}; verify its selectors and namespace label "
            f"{_METRICS_SCRAPER_LABEL}=true, or specify a compatible instance "
            "with --prometheus NAMESPACE/NAME"
        )
    if len(compatible) > 1:
        instances = ", ".join(
            f"{ref.namespace}/{ref.name}" for ref in compatible
        )
        raise DeploymentError(
            f"multiple compatible Prometheus instances found: {instances}; "
            "select one with --prometheus NAMESPACE/NAME"
        )
    return compatible[0]


def prometheus_selects_service_monitor(
    kubectl: Kubectl,
    prometheus: PrometheusRef,
    monitor_namespace: str,
    monitor_labels: tuple[tuple[str, str], ...],
) -> bool:
    """Return whether a reused Prometheus selects one existing ServiceMonitor."""
    value = kubectl.get(
        "prometheuses.monitoring.coreos.com",
        prometheus.name,
        prometheus.namespace,
    )
    spec = value.get("spec") or {}
    if not isinstance(spec, dict):
        return False
    namespace = kubectl.get_if_exists("namespace", monitor_namespace) or {}
    namespace_labels = _namespace_labels(namespace, monitor_namespace)
    return matches_label_selector(
        spec.get("serviceMonitorSelector"), dict(monitor_labels)
    ) and _namespacematches_label_selector(
        spec,
        "serviceMonitorNamespaceSelector",
        prometheus.namespace,
        monitor_namespace,
        namespace_labels,
    )


def _requested_identity(requested: str | None) -> tuple[str, str] | None:
    """Parse the optional Prometheus identity supplied by the install command."""
    if requested is None:
        return None
    namespace, separator, name = requested.partition("/")
    if not separator or not namespace or not name or "/" in name:
        raise DeploymentError(
            "--prometheus must use NAMESPACE/NAME, such as monitoring/prometheus"
        )
    return namespace, name


def _identity(value: dict[str, Any]) -> tuple[str, str] | None:
    """Return a Prometheus object's namespace and name when both are present."""
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return None
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not isinstance(namespace, str) or not isinstance(name, str):
        return None
    if not namespace or not name:
        return None
    return namespace, name


def _prometheus_namespace_is_trusted(
    kubectl: Kubectl, value: dict[str, Any]
) -> bool:
    """Return whether a Prometheus namespace may scrape model-server metrics."""
    identity = _identity(value)
    if identity is None:
        return False
    namespace = kubectl.get_if_exists("namespace", identity[0]) or {}
    metadata = namespace.get("metadata") or {}
    labels = metadata.get("labels") or {}
    return (
        isinstance(labels, dict)
        and labels.get(_METRICS_SCRAPER_LABEL) == "true"
    )


def _available_identities(values: tuple[dict[str, Any], ...]) -> str:
    """Format usable Prometheus identities for an actionable selection error."""
    identities = sorted(
        identity
        for value in values
        for identity in (_identity(value),)
        if identity is not None
    )
    return ", ".join(f"{namespace}/{name}" for namespace, name in identities)


def _namespace_labels(value: dict[str, Any], namespace: str) -> dict[str, str]:
    """Return platform namespace labels, preserving Kubernetes' name label."""
    labels = {"kubernetes.io/metadata.name": namespace}
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return labels
    source_labels = metadata.get("labels")
    if not isinstance(source_labels, dict):
        return labels
    labels.update(
        {
            key: label
            for key, label in source_labels.items()
            if isinstance(key, str) and isinstance(label, str)
        }
    )
    return labels


def _compatible_prometheus(
    value: dict[str, Any],
    platform_namespace: str,
    namespace_labels: dict[str, str],
) -> PrometheusRef | None:
    """Return one ready Prometheus when it selects both Foretoken resources."""
    identity = _identity(value)
    spec = value.get("spec")
    status = value.get("status")
    if identity is None or not isinstance(spec, dict) or not isinstance(status, dict):
        return None
    if spec.get("paused") is True or spec.get("ignoreNamespaceSelectors") is True:
        return None
    available_replicas = status.get("availableReplicas")
    if not isinstance(available_replicas, int) or available_replicas <= 0:
        return None

    service_selector = spec.get("serviceMonitorSelector")
    rule_selector = spec.get("ruleSelector")
    additional_labels = _additional_labels(service_selector, rule_selector)
    if additional_labels is None:
        return None
    resource_labels = dict(_FORETOKEN_LABELS)
    resource_labels.update(additional_labels)
    if not matches_label_selector(service_selector, resource_labels):
        return None
    if not matches_label_selector(rule_selector, resource_labels):
        return None

    prometheus_namespace, name = identity
    if not _namespacematches_label_selector(
        spec,
        "serviceMonitorNamespaceSelector",
        prometheus_namespace,
        platform_namespace,
        namespace_labels,
    ):
        return None
    if not _namespacematches_label_selector(
        spec,
        "ruleNamespaceSelector",
        prometheus_namespace,
        platform_namespace,
        namespace_labels,
    ):
        return None
    return PrometheusRef(name, prometheus_namespace, additional_labels)


def _additional_labels(
    service_selector: Any, rule_selector: Any
) -> tuple[tuple[str, str], ...] | None:
    """Merge selector matchLabels without overriding Foretoken's fixed labels."""
    labels: dict[str, str] = {}
    for selector in (service_selector, rule_selector):
        if not isinstance(selector, dict):
            return None
        match_labels = selector.get("matchLabels", {})
        match_expressions = selector.get("matchExpressions", [])
        if not isinstance(match_labels, dict) or not isinstance(
            match_expressions, list
        ):
            return None
        for key, value in match_labels.items():
            if not isinstance(key, str) or not isinstance(value, str):
                return None
            if key in _FORETOKEN_LABELS:
                if _FORETOKEN_LABELS[key] != value:
                    return None
                continue
            existing = labels.get(key)
            if existing is not None and existing != value:
                return None
            labels[key] = value
    return tuple(sorted(labels.items()))


def _namespacematches_label_selector(
    spec: dict[str, Any],
    selector_name: str,
    prometheus_namespace: str,
    platform_namespace: str,
    platform_labels: dict[str, str],
) -> bool:
    """Apply Prometheus' default same-namespace behavior for missing selectors."""
    if selector_name not in spec or spec[selector_name] is None:
        return prometheus_namespace == platform_namespace
    return matches_label_selector(spec[selector_name], platform_labels)


def matches_label_selector(selector: Any, labels: dict[str, str]) -> bool:
    """Evaluate the Kubernetes LabelSelector forms used by Prometheus resources."""
    if not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        return False
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or labels.get(key) != value
        for key, value in match_labels.items()
    ):
        return False
    return all(_expression_matches(expression, labels) for expression in expressions)


def _expression_matches(expression: Any, labels: dict[str, str]) -> bool:
    """Evaluate one Kubernetes label-selection expression against resource labels."""
    if not isinstance(expression, dict):
        return False
    key = expression.get("key")
    operator = expression.get("operator")
    if not isinstance(key, str) or not isinstance(operator, str):
        return False
    if operator == "Exists":
        return key in labels
    if operator == "DoesNotExist":
        return key not in labels

    values = expression.get("values")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        return False
    label = labels.get(key)
    if operator == "In":
        return label in values
    if operator == "NotIn":
        return label not in values
    return False
