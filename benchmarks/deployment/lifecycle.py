# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Deploy missing Foretoken services for one benchmark and clean up owned objects."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml

from benchmarks.deployment.cluster import Kubectl
from benchmarks.deployment.discovery import BenchmarkEndpoint, discover_endpoint
from benchmarks.deployment.manifest import (
    DeploymentError,
    DeploymentResources,
    deployment_path,
    parse_deployment,
)

logger = logging.getLogger(__name__)


def _object_identity(document: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(document.get("kind") or "").strip()
    metadata = document.get("metadata") or {}
    name = str(metadata.get("name") or "").strip()
    namespace = str(metadata.get("namespace") or "").strip()
    if not kind or not name:
        raise DeploymentError(
            "each rendered Kubernetes object requires kind and metadata.name"
        )
    return kind, name, namespace


def _service_presence(resources: DeploymentResources, kubectl: Kubectl) -> list[bool]:
    return [
        kubectl.exists("frontendservice", resources.frontend, resources.namespace),
        *(
            kubectl.exists("modelservice", name, resources.namespace)
            for name in resources.models
        ),
    ]


def _new_objects(
    resources: DeploymentResources, kubectl: Kubectl
) -> tuple[dict[str, Any], ...]:
    created: list[dict[str, Any]] = []
    for document in resources.objects:
        kind, name, namespace = _object_identity(document)
        if not kubectl.exists(kind, name, namespace):
            created.append(document)
    return tuple(created)


def _delete_objects(
    kubectl: Kubectl, objects: tuple[dict[str, Any], ...], timeout: str
) -> None:
    namespaced = tuple(
        document for document in objects if document.get("kind") != "Namespace"
    )
    namespaces = tuple(
        document for document in objects if document.get("kind") == "Namespace"
    )
    for group in (namespaced, namespaces):
        if group:
            kubectl.delete_yaml(yaml.safe_dump_all(group), timeout)


@contextmanager
def benchmark_deployment(
    deployment: str,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> Iterator[BenchmarkEndpoint]:
    """Reuse an existing Foretoken deployment or own it for this benchmark."""
    kubectl = Kubectl()
    path = deployment_path(deployment)
    resources = parse_deployment(path, kubectl.kustomize(path))
    presence = _service_presence(resources, kubectl)
    if any(presence) and not all(presence):
        raise DeploymentError(
            "deployment is partially present; apply or delete it explicitly before benchmarking"
        )

    created: tuple[dict[str, Any], ...] = ()
    if not any(presence):
        created = _new_objects(resources, kubectl)
        logger.info("Deploying Foretoken service from %s", path)
        try:
            kubectl.apply_kustomize(path)
        except Exception:
            _delete_objects(kubectl, created, timeout)
            raise

    try:
        yield discover_endpoint(
            resources,
            kubectl,
            timeout,
            requested_model=requested_model,
            api_key=api_key,
        )
    finally:
        if created:
            logger.info("Cleaning up Foretoken service from %s", path)
            _delete_objects(kubectl, created, timeout)
