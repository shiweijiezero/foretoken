# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Manage only the metrics-scraper namespace access granted by platform installation."""

from __future__ import annotations

from foretoken.kubernetes.client import Kubectl
from foretoken.observability import METRICS_SCRAPER_LABEL

_METRICS_SCRAPER_OWNER_ANNOTATION = "inference.foretoken.io/metrics-scraper-managed-by"
_METRICS_SCRAPER_OWNER = "foretoken"
_LEGACY_METRICS_SCRAPER_OWNER = "foretoken-cli"


def mark_managed_metrics_scraper_namespace(kubectl: Kubectl, name: str) -> None:
    """Grant metrics access and record ownership only when the label was absent."""
    namespace = kubectl.get("namespace", name)
    metadata = namespace.get("metadata") or {}
    labels = metadata.get("labels") or {}
    if isinstance(labels, dict) and labels.get(METRICS_SCRAPER_LABEL) == "true":
        return
    kubectl.annotate_namespace(name, _METRICS_SCRAPER_OWNER_ANNOTATION, _METRICS_SCRAPER_OWNER)
    kubectl.label_namespace(name, METRICS_SCRAPER_LABEL, "true")


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
    kubectl.remove_namespace_label(name, METRICS_SCRAPER_LABEL)
    kubectl.remove_namespace_annotation(name, _METRICS_SCRAPER_OWNER_ANNOTATION)
