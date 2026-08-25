# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Public endpoint and model discovery for deployment benchmarks."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from benchmarks.deployment.cluster import (
    Kubectl,
    NetworkAddress,
    find_address,
    timeout_seconds,
    wait_for_deployment,
)
from benchmarks.deployment.manifest import DeploymentError, DeploymentResources


@dataclass(frozen=True)
class BenchmarkEndpoint:
    """Public request target discovered from a Foretoken deployment."""

    url: str
    model: str
    models: tuple[str, ...]
    headers: dict[str, str]
    hostname: str


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


def _chat_url(address: NetworkAddress) -> str:
    default_port = 443 if address.scheme == "https" else 80
    host = address.host
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if address.port != default_port:
        authority = f"{authority}:{address.port}"
    return f"{address.scheme}://{authority}/v1/chat/completions"


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
    resources: DeploymentResources,
    kubectl: Kubectl,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> BenchmarkEndpoint:
    """Find the endpoint and model for rendered Foretoken resources."""
    wait_seconds = timeout_seconds(timeout)
    model = _select_model(resources.models.values(), requested_model)
    wait_for_deployment(resources, kubectl, timeout)
    address = find_address(resources, kubectl, wait_seconds)
    url = _chat_url(address)
    headers = {"Host": address.routing_host} if address.routing_host else {}
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
    return BenchmarkEndpoint(url, model, models, headers, resources.hostname)
