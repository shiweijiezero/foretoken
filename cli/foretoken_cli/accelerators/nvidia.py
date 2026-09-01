# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""NVIDIA GPU and DCGM Exporter discovery for platform installation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.observability import matches_label_selector

_GPU_RESOURCE = "nvidia.com/gpu"
_GPU_NODE_LABELS = (
    "nvidia.com/gpu.present",
    "feature.node.kubernetes.io/pci-10de.present",
)
_TOLERATED_TAINTS = {
    ("node-role.kubernetes.io/control-plane", "NoSchedule"),
    ("node.kubernetes.io/disk-pressure", "NoSchedule"),
    ("node.kubernetes.io/memory-pressure", "NoSchedule"),
    ("node.kubernetes.io/not-ready", "NoExecute"),
    ("node.kubernetes.io/pid-pressure", "NoSchedule"),
    ("node.kubernetes.io/unreachable", "NoExecute"),
    ("node.kubernetes.io/unschedulable", "NoSchedule"),
}


@dataclass(frozen=True)
class NvidiaMetrics:
    """The reusable exporter or safe placement for a managed release."""

    reused_daemonset: ResourceRef | None
    reused_service_monitor: ResourceRef | None
    service_monitor_labels: tuple[tuple[str, str], ...]
    node_selector: tuple[str, str] | None


def discover_nvidia_metrics(
    kubectl: Kubectl, managed_daemonset: ResourceRef | None = None
) -> NvidiaMetrics | None:
    """Resolve NVIDIA GPU nodes and a safe DCGM Exporter lifecycle."""
    candidate_nodes = tuple(
        node
        for node in kubectl.list_all_resources(("node",))
        if not (node.get("spec") or {}).get("unschedulable")
    )
    blocked_gpu_nodes = tuple(
        name
        for node in candidate_nodes
        if _positive_gpu_capacity(node) and _unsupported_taints(node)
        for name in (_object_name(node),)
        if name
    )
    if blocked_gpu_nodes:
        raise DeploymentError(
            "NVIDIA GPU nodes have NoSchedule or NoExecute taints that the managed "
            "DCGM Exporter does not tolerate: " + ", ".join(sorted(blocked_gpu_nodes))
        )
    nodes = tuple(node for node in candidate_nodes if not _unsupported_taints(node))
    gpu_nodes = _gpu_node_names(nodes)
    if not gpu_nodes:
        return None

    candidates = tuple(
        daemonset
        for daemonset in kubectl.list_all_resources(("daemonset.apps",))
        if _is_dcgm_exporter(daemonset)
    )
    if managed_daemonset is not None:
        external = tuple(
            ref
            for value in candidates
            for ref in (_resource_ref(value),)
            if ref is not None and ref != managed_daemonset
        )
        if external:
            names = ", ".join(
                f"{ref.namespace}/{ref.display_name}" for ref in external
            )
            raise DeploymentError(
                "a CLI-managed DCGM Exporter cannot coexist with another exporter: "
                + names
            )
        return NvidiaMetrics(None, None, (), _managed_node_selector(nodes, gpu_nodes))

    compatible = tuple(
        (daemonset, ref)
        for daemonset in candidates
        if _daemonset_covers_nodes(kubectl, daemonset, set(gpu_nodes))
        for ref in (_resource_ref(daemonset),)
        if ref is not None
    )
    if len(compatible) > 1:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}" for _, ref in compatible
        )
        raise DeploymentError(
            f"multiple ready DCGM Exporters cover NVIDIA GPU nodes: {names}"
        )
    if compatible:
        daemonset, daemonset_ref = compatible[0]
        service_monitor, labels = _dcgm_service_monitor(
            kubectl, daemonset, daemonset_ref
        )
        return NvidiaMetrics(daemonset_ref, service_monitor, labels, None)
    if candidates:
        names = ", ".join(
            f"{ref.namespace}/{ref.display_name}"
            for value in candidates
            for ref in (_resource_ref(value),)
            if ref is not None
        )
        raise DeploymentError(
            "an existing DCGM Exporter does not cover every NVIDIA GPU node; "
            f"repair its lifecycle before installing Foretoken: {names}"
        )
    return NvidiaMetrics(None, None, (), _managed_node_selector(nodes, gpu_nodes))


def _managed_node_selector(
    nodes: tuple[dict[str, Any], ...], gpu_nodes: tuple[str, ...]
) -> tuple[str, str] | None:
    """Return a proven GPU-only node selector for a managed exporter."""
    schedulable_nodes = {
        name for node in nodes for name in (_object_name(node),) if name
    }
    gpu_node_set = set(gpu_nodes)
    if schedulable_nodes == gpu_node_set:
        return None
    for label in _GPU_NODE_LABELS:
        selected = {
            name
            for node in nodes
            if _node_label(node, label) == "true"
            for name in (_object_name(node),)
            if name
        }
        if selected == gpu_node_set:
            return label, "true"
    raise DeploymentError(
        "NVIDIA GPU nodes need a GPU-only label before Foretoken can install "
        "DCGM Exporter; use nvidia.com/gpu.present=true or "
        "feature.node.kubernetes.io/pci-10de.present=true"
    )


def _unsupported_taints(node: dict[str, Any]) -> tuple[str, ...]:
    """Return scheduling taints not covered by the managed chart defaults."""
    taints = (node.get("spec") or {}).get("taints") or []
    return tuple(
        str(taint.get("key") or "")
        for taint in taints
        if isinstance(taint, dict)
        and taint.get("effect") in {"NoSchedule", "NoExecute"}
        and (str(taint.get("key") or ""), str(taint.get("effect") or ""))
        not in _TOLERATED_TAINTS
    )


def _gpu_node_names(nodes: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    """Return stable names for nodes that advertise NVIDIA GPU capacity."""
    return tuple(
        sorted(
            name
            for node in nodes
            if _positive_gpu_capacity(node)
            for name in (_object_name(node),)
            if name
        )
    )


def _positive_gpu_capacity(node: dict[str, Any]) -> bool:
    """Return whether Kubernetes advertises an allocatable NVIDIA GPU."""
    value = ((node.get("status") or {}).get("allocatable") or {}).get(
        _GPU_RESOURCE
    )
    try:
        return int(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _object_name(value: dict[str, Any]) -> str:
    """Return one Kubernetes object's name when present."""
    return str((value.get("metadata") or {}).get("name") or "")


def _node_label(node: dict[str, Any], key: str) -> str:
    """Return one node label value when represented as a string."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return ""
    return str(labels.get(key) or "")


def _resource_ref(value: dict[str, Any]) -> ResourceRef | None:
    """Return one namespaced resource identity when complete."""
    metadata = value.get("metadata") or {}
    kind = str(value.get("kind") or "")
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    if not kind or not name:
        return None
    return ResourceRef(kind, name, namespace)


def _is_dcgm_exporter(daemonset: dict[str, Any]) -> bool:
    """Recognize standalone and GPU Operator exporter identities."""
    metadata = daemonset.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "")
    return isinstance(labels, dict) and (
        name == "nvidia-dcgm-exporter"
        or labels.get("app.kubernetes.io/name") == "dcgm-exporter"
        or labels.get("app") in {"dcgm-exporter", "nvidia-dcgm-exporter"}
    )


def _daemonset_covers_nodes(
    kubectl: Kubectl, daemonset: dict[str, Any], gpu_nodes: set[str]
) -> bool:
    """Return whether ready exporter Pods cover every NVIDIA GPU node."""
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
    return gpu_nodes.issubset(ready_nodes)


def _dcgm_service_monitor(
    kubectl: Kubectl,
    daemonset: dict[str, Any],
    daemonset_ref: ResourceRef,
) -> tuple[ResourceRef, tuple[tuple[str, str], ...]]:
    """Return the ServiceMonitor proven to scrape one reusable exporter."""
    resources = set(kubectl.api_resource_names("monitoring.coreos.com"))
    if "servicemonitors.monitoring.coreos.com" not in resources:
        raise DeploymentError(
            f"reused DCGM Exporter {daemonset_ref.namespace}/{daemonset_ref.name} "
            "has no Prometheus Operator API for its ServiceMonitor"
        )
    pod_labels = (
        (((daemonset.get("spec") or {}).get("template") or {}).get("metadata") or {})
        .get("labels")
        or {}
    )
    if not isinstance(pod_labels, dict):
        raise DeploymentError("DCGM Exporter Pod labels are invalid")
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
        for ref in (_resource_ref(monitor),)
        if ref is not None
        and any(_monitor_selects_service(monitor, service) for service in services)
    }
    if len(monitors) != 1:
        raise DeploymentError(
            f"reused DCGM Exporter {daemonset_ref.namespace}/{daemonset_ref.name} "
            "needs exactly one ServiceMonitor that selects its metrics Service"
        )
    ref, monitor = next(iter(monitors.items()))
    labels = (monitor.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise DeploymentError("DCGM Exporter ServiceMonitor metadata is invalid")
    return ref, tuple(sorted(labels.items()))


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
    monitor: dict[str, Any], service: dict[str, Any]
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
        isinstance(endpoint, dict) and endpoint.get("port") in service_ports
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
