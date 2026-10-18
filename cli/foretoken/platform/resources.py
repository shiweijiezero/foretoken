# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Discover platform consumers and manage metrics-scraper namespace access."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from foretoken.kubernetes.client import Kubectl, ResourceRef, resource_ref


_METRICS_SCRAPER_LABEL = "inference.foretoken.io/metrics-scraper"
_METRICS_SCRAPER_OWNER_ANNOTATION = (
    "inference.foretoken.io/metrics-scraper-managed-by"
)
_METRICS_SCRAPER_OWNER = "foretoken"
_LEGACY_METRICS_SCRAPER_OWNER = "foretoken-cli"


def _resource_refs(values: Iterable[dict[str, Any]]) -> tuple[ResourceRef, ...]:
    """Return named resource identities from Kubernetes objects."""
    return tuple(resource_ref(value) for value in values)


def control_plane_deployments(kubectl: Kubectl) -> tuple[ResourceRef, ...]:
    """Return active Foretoken control-plane Deployments across the cluster."""
    return _resource_refs(
        kubectl.list_all_resources(
            ("deployment.apps",),
            label_selector="app.kubernetes.io/name=foretoken-control-plane",
        )
    )


def mark_managed_metrics_scraper_namespace(kubectl: Kubectl, name: str) -> None:
    """Grant metrics access and record ownership when the label was absent."""
    namespace = kubectl.get("namespace", name)
    metadata = namespace.get("metadata") or {}
    labels = metadata.get("labels") or {}
    if isinstance(labels, dict) and labels.get(_METRICS_SCRAPER_LABEL) == "true":
        return
    kubectl.annotate_namespace(
        name,
        _METRICS_SCRAPER_OWNER_ANNOTATION,
        _METRICS_SCRAPER_OWNER,
    )
    kubectl.label_namespace(name, _METRICS_SCRAPER_LABEL, "true")


def unmark_managed_metrics_scraper_namespace(kubectl: Kubectl, name: str) -> None:
    """Remove metrics access only when the namespace records CLI ownership."""
    namespace = kubectl.get_if_exists("namespace", name)
    if namespace is None:
        return
    metadata = namespace.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    if (
        not isinstance(annotations, dict)
        or annotations.get(_METRICS_SCRAPER_OWNER_ANNOTATION)
        not in {_METRICS_SCRAPER_OWNER, _LEGACY_METRICS_SCRAPER_OWNER}
    ):
        return
    kubectl.remove_namespace_label(name, _METRICS_SCRAPER_LABEL)
    kubectl.remove_namespace_annotation(name, _METRICS_SCRAPER_OWNER_ANNOTATION)


def platform_service_resources(kubectl: Kubectl) -> tuple[ResourceRef, ...]:
    """Find Foretoken reconciliation resources and workloads before migrating or removing their platform."""
    supported = set(kubectl.api_resource_names())
    kinds = tuple(sorted(name for name in supported if name.endswith(".inference.foretoken.io")))
    objects = list(kubectl.list_all_resources(kinds)) if kinds else []
    workloads = ("pods", "jobs", "replicasets", "deployments", "statefulsets")
    if "leaderworkersets.leaderworkerset.x-k8s.io" in supported:
        workloads += ("leaderworkersets.leaderworkerset.x-k8s.io",)
    # Deleting a service does not synchronously terminate its descendants.
    for label in (
        "inference.foretoken.io/frontend-service",
        "inference.foretoken.io/model-group",
        "inference.foretoken.io/model-preparation-group",
    ):
        objects.extend(
            kubectl.list_all_resources(
                workloads,
                label_selector=label,
            )
        )
    return tuple(dict.fromkeys(_resource_refs(objects)))
