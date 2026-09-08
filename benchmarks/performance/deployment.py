# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""为 HTTP 性能评测发现、临时部署并清理 Foretoken 服务。"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from foretoken.kubernetes import (
    FrontendEndpoint,
    Kubectl,
    load_deployment,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import DeploymentError, ForetokenDeployment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeployedBenchmarkEndpoint:
    """保存从 Foretoken 部署发现的公开 Chat Completions 测量目标。"""

    url: str
    model: str
    models: tuple[str, ...]
    headers: dict[str, str]
    hostname: str
    gpu_count: int


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


def _chat_url(endpoint: FrontendEndpoint) -> str:
    return f"{endpoint.url}/v1/chat/completions"


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


def select_benchmark_model(
    deployment: ForetokenDeployment,
    requested_model: str,
) -> tuple[str, int]:
    """返回本次性能评测选择的公开模型和部署 GPU 总数。"""
    model = _select_model(deployment.models.values(), requested_model)
    return model, _model_gpu_count(deployment, model)


def _api_root(chat_url: str) -> str:
    parsed = urlsplit(chat_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _wait_for_models(
    url: str,
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
                response = client.get(f"{_api_root(url)}/v1/models")
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


def discover_benchmark_endpoint(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> DeployedBenchmarkEndpoint:
    """等待已渲染服务就绪并返回公开的 HTTP 性能测量目标。"""
    wait_seconds = timeout_seconds(timeout)
    model, gpu_count = select_benchmark_model(deployment, requested_model)
    wait_for_resources(deployment.service_refs(), kubectl, timeout)
    endpoint = resolve_frontend_endpoint(deployment, kubectl, timeout)
    url = _chat_url(endpoint)
    headers = {"Host": endpoint.routing_host} if endpoint.routing_host else {}
    models = _wait_for_models(
        url,
        headers,
        wait_seconds,
        deployment.models.values(),
        api_key,
    )
    if model not in models:
        raise DeploymentError(
            f"model {model!r} is not advertised by the frontend; "
            f"available models: {', '.join(models)}"
        )
    return DeployedBenchmarkEndpoint(
        url,
        model,
        models,
        headers,
        deployment.hostname,
        gpu_count,
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
    """先删除命名空间内对象，再删除本次评测创建的 Namespace。"""
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
def benchmark_endpoint_from_kustomize(
    kustomize_path: str,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> Iterator[DeployedBenchmarkEndpoint]:
    """复用完整部署，或只创建并清理本次性能评测缺失的对象。"""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
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
            _delete_objects(kubectl, created, timeout)
            raise

    try:
        yield discover_benchmark_endpoint(
            deployment,
            kubectl,
            timeout,
            requested_model=requested_model,
            api_key=api_key,
        )
    finally:
        if created:
            logger.info("Cleaning up Foretoken service from %s", deployment.path)
            _delete_objects(kubectl, created, timeout)
