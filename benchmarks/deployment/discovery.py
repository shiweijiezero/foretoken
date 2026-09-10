# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Public endpoint and model discovery for deployment benchmarks."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from foretoken.kubernetes import (
    FrontendEndpoint,
    Kubectl,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import DeploymentError, ForetokenDeployment


@dataclass(frozen=True)
class BenchmarkEndpoint:
    """Public request target discovered from a Foretoken deployment."""

    url: str
    model: str
    models: tuple[str, ...]
    headers: dict[str, str]
    hostname: str
    gpu_count: int
    namespace: str
    model_services: tuple[str, ...]


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


def _model_gpu_count(
    deployment: ForetokenDeployment, model: str
) -> int:
    """Return configured GPUs across ModelServices serving one model."""
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


def resolve_deployment_model(
    deployment: ForetokenDeployment, requested_model: str
) -> tuple[str, int]:
    """Return the selected model and configured GPU capacity."""
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
        headers=request_headers, timeout=5.0, follow_redirects=True
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


def discover_endpoint(
    resources: ForetokenDeployment,
    kubectl: Kubectl,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> BenchmarkEndpoint:
    """Find the endpoint and model for rendered Foretoken resources."""
    wait_seconds = timeout_seconds(timeout)
    model, gpu_count = resolve_deployment_model(resources, requested_model)
    wait_for_resources(resources.service_refs(), kubectl, timeout)
    endpoint = resolve_frontend_endpoint(resources, kubectl, timeout)
    url = _chat_url(endpoint)
    headers = {"Host": endpoint.routing_host} if endpoint.routing_host else {}
    models = _wait_for_models(
        url,
        headers,
        wait_seconds,
        resources.models.values(),
        api_key,
    )
    if model not in models:
        raise DeploymentError(
            f"model {model!r} is not advertised by the frontend; "
            f"available models: {', '.join(models)}"
        )
    return BenchmarkEndpoint(
        url,
        model,
        models,
        headers,
        resources.hostname,
        gpu_count,
        resources.namespace,
        tuple(
            sorted(
                name
                for name, declared_model in resources.models.items()
                if declared_model == model
            )
        ),
    )
