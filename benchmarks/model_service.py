# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve the model service a benchmark measures, including temporary Kustomize deployments."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from benchmarks.config.benchmark import ModelServiceSource
from foretoken.kubernetes import (
    Kubectl,
    load_deployment,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import DeploymentError, ForetokenDeployment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelService:
    """Describe one OpenAI-compatible model service that is ready for benchmark requests.

    ``chat_completions_url`` preserves the public endpoint selected by the user or
    deployment. ``routing_host`` is the HTTP ``Host`` header a Gateway HTTP listener
    needs to route by hostname; it is empty for LoadBalancer, HTTPS, and user-supplied
    URLs. Every client derives its ``Authorization`` header from ``api_key``.
    """

    chat_completions_url: str
    model: str
    api_key: str
    models: tuple[str, ...]
    hostname: str
    gpu_count: int
    routing_host: str

    @property
    def api_root(self) -> str:
        """Return the OpenAI client base URL derived from the Chat Completions endpoint."""
        return self.chat_completions_url.rstrip("/").removesuffix(
            "/chat/completions"
        )

    @property
    def request_headers(self) -> dict[str, str]:
        """Return the routing headers every request must carry, without credentials."""
        return {"Host": self.routing_host} if self.routing_host else {}


def _select_model(models: Iterable[str], requested: str) -> str:
    available = sorted(set(models))
    if requested:
        if requested not in available:
            raise DeploymentError(
                f"model {requested!r} is not declared by the deployment; "
                f"available models: {', '.join(available)}"
            )
        return requested
    if len(available) != 1:
        raise DeploymentError(
            "the deployment declares multiple models; pass --model with one of: "
            + ", ".join(available)
        )
    return available[0]


def _model_gpu_count(deployment: ForetokenDeployment, model: str) -> int:
    total = 0
    for document in deployment.objects:
        if document.get("kind") != "ModelService":
            continue
        spec = document.get("spec") or {}
        if str(spec.get("model") or "") != model:
            continue
        requests = ((spec.get("resources") or {}).get("requests") or {})
        gpu_count = int((requests.get("gpu") or {}).get("count") or 0)
        replicas = int(spec.get("replicas") or 1)
        nodes = int(spec.get("nodes") or 1)
        total += gpu_count * replicas * nodes
    if total < 1:
        raise DeploymentError(
            f"deployment does not declare GPU capacity for model {model!r}"
        )
    return total


def _wait_for_models(
    api_root: str,
    headers: dict[str, str],
    timeout: float,
    expected_models: Iterable[str],
    api_key: str,
) -> tuple[str, ...]:
    expected = set(expected_models)
    deadline = time.monotonic() + timeout
    request_headers = {**headers, "Authorization": f"Bearer {api_key}"}
    last_error = "frontend has not responded"
    with httpx.Client(
        headers=request_headers,
        timeout=5.0,
        follow_redirects=True,
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{api_root}/models")
                response.raise_for_status()
                models = tuple(
                    sorted(
                        str(item["id"])
                        for item in response.json().get("data", [])
                        if isinstance(item, dict) and item.get("id")
                    )
                )
                if expected.issubset(models):
                    return models
                last_error = (
                    f"frontend advertises {models or '<no models>'}; "
                    f"waiting for {tuple(sorted(expected))}"
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = str(exc)
            time.sleep(2)
    raise DeploymentError(f"frontend did not become ready: {last_error}")


def _discover_model_service(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    source: ModelServiceSource,
) -> ModelService:
    """Wait for the rendered service to become ready and return its public model service."""
    wait_seconds = timeout_seconds(source.wait_timeout)
    model = _select_model(deployment.models.values(), source.model)
    gpu_count = _model_gpu_count(deployment, model)
    wait_for_resources(deployment.service_refs(), kubectl, source.wait_timeout)
    endpoint = resolve_frontend_endpoint(deployment, kubectl, source.wait_timeout)
    chat_completions_url = f"{endpoint.url}/v1/chat/completions"
    api_root = f"{endpoint.url}/v1"
    headers = {"Host": endpoint.routing_host} if endpoint.routing_host else {}
    models = _wait_for_models(
        api_root,
        headers,
        wait_seconds,
        deployment.models.values(),
        source.api_key,
    )
    if model not in models:
        raise DeploymentError(
            f"model {model!r} is not advertised by the frontend; "
            f"available models: {', '.join(models)}"
        )
    return ModelService(
        chat_completions_url=chat_completions_url,
        model=model,
        api_key=source.api_key,
        models=models,
        hostname=deployment.hostname,
        gpu_count=gpu_count,
        routing_host=endpoint.routing_host,
    )


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


def _service_presence(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
) -> list[bool]:
    return [
        kubectl.exists(resource.kind, resource.name, resource.namespace)
        for resource in deployment.service_refs()
    ]


def _missing_objects(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
) -> tuple[dict[str, Any], ...]:
    service_objects = {
        (resource.kind, resource.name, resource.namespace)
        for resource in deployment.service_refs()
    }
    created: list[dict[str, Any]] = []
    for document in deployment.objects:
        identity = _object_identity(document)
        if identity in service_objects or not kubectl.exists(*identity):
            created.append(document)
    return tuple(created)


def _delete_objects(
    kubectl: Kubectl,
    objects: tuple[dict[str, Any], ...],
    timeout: str,
) -> None:
    """Delete namespaced objects first, then delete the Namespace created for this benchmark."""
    namespaced = tuple(
        document for document in objects if document.get("kind") != "Namespace"
    )
    namespaces = tuple(
        document for document in objects if document.get("kind") == "Namespace"
    )
    for group in (namespaced, namespaces):
        if group:
            kubectl.delete(yaml.safe_dump_all(group), timeout)


@contextmanager
def resolve_model_service(source: ModelServiceSource) -> Iterator[ModelService]:
    """Yield the model service selected by the benchmark user.

    A URL source is used as given without touching Kubernetes. A Kustomize source
    reuses a complete deployment unchanged, or creates only the missing objects
    and deletes them again after the benchmark; a partially present deployment is
    rejected.
    """
    if source.url:
        yield ModelService(
            chat_completions_url=source.url,
            model=source.model,
            api_key=source.api_key,
            models=(source.model,),
            hostname="",
            gpu_count=1,
            routing_host="",
        )
        return

    kubectl = Kubectl()
    deployment = load_deployment(source.kustomize_path, kubectl)
    presence = _service_presence(deployment, kubectl)
    if any(presence) and not all(presence):
        raise DeploymentError(
            "The Foretoken deployment is only partially present. "
            "Apply or delete it, then rerun the benchmark"
        )

    created: tuple[dict[str, Any], ...] = ()
    if not any(presence):
        created = _missing_objects(deployment, kubectl)
        logger.info("Deploying Foretoken service from %s", deployment.path)
        try:
            kubectl.apply(yaml.safe_dump_all(created))
        except Exception:
            _delete_objects(kubectl, created, source.wait_timeout)
            raise

    try:
        yield _discover_model_service(deployment, kubectl, source)
    finally:
        if created:
            logger.info("Cleaning up Foretoken service from %s", deployment.path)
            _delete_objects(kubectl, created, source.wait_timeout)
