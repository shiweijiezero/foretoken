# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Public endpoint and model discovery for deployment benchmarks."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from foretoken_cli.kubernetes import Kubectl, timeout_seconds, wait_for_resources
from foretoken_cli.manifest import DeploymentError, ForetokenDeployment


@dataclass(frozen=True)
class NetworkAddress:
    """Public network address and optional HTTP routing hostname."""

    scheme: str
    host: str
    port: int
    routing_host: str = ""


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


def _wait_for_object(
    fetch: Callable[[], dict[str, Any]],
    ready: Callable[[dict[str, Any]], bool],
    timeout: float,
    description: str,
) -> dict[str, Any]:
    """Poll one benchmark endpoint dependency until it publishes an address."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fetch()
        if ready(value):
            return value
        time.sleep(2)
    raise DeploymentError(f"timed out waiting for {description}")


def _load_balancer_address(
    service: dict[str, Any],
    resources: ForetokenDeployment,
    kubectl: Kubectl,
    timeout: float,
) -> NetworkAddress:
    """Resolve the frontend's controller-owned LoadBalancer Service address."""

    def has_ingress(value: dict[str, Any]) -> bool:
        ingress = ((value.get("status") or {}).get("loadBalancer") or {}).get(
            "ingress"
        )
        return bool(ingress)

    if not has_ingress(service):
        service = _wait_for_object(
            lambda: kubectl.get("service", resources.frontend, resources.namespace),
            has_ingress,
            timeout,
            f"LoadBalancer address for service/{resources.frontend}",
        )
    ingress = service["status"]["loadBalancer"]["ingress"][0]
    address = str(ingress.get("ip") or ingress.get("hostname") or "").strip()
    if not address:
        raise DeploymentError(
            f"service/{resources.frontend} has an empty LoadBalancer address"
        )
    ports = (service.get("spec") or {}).get("ports") or []
    http_port = next(
        (item for item in ports if item.get("name") == "http"),
        ports[0] if ports else None,
    )
    if not http_port or not http_port.get("port"):
        raise DeploymentError(f"service/{resources.frontend} has no public HTTP port")
    return NetworkAddress("http", address, int(http_port["port"]))


def _gateway_address(
    resources: ForetokenDeployment,
    kubectl: Kubectl,
    timeout: float,
) -> NetworkAddress:
    """Resolve the Gateway selected by the frontend's controller-owned HTTPRoute."""
    if not resources.hostname:
        raise DeploymentError("gateway frontend does not declare spec.hostname")
    route = kubectl.get("httproute", resources.frontend, resources.namespace)
    parents = (route.get("spec") or {}).get("parentRefs") or []
    parent = next(
        (
            item
            for item in parents
            if item.get("kind", "Gateway") == "Gateway"
            and item.get("group", "gateway.networking.k8s.io")
            == "gateway.networking.k8s.io"
        ),
        None,
    )
    if not parent or not parent.get("name"):
        raise DeploymentError(f"httproute/{resources.frontend} has no Gateway parent")

    gateway_name = str(parent["name"])
    gateway_namespace = str(parent.get("namespace") or resources.namespace)
    gateway = _wait_for_object(
        lambda: kubectl.get("gateway", gateway_name, gateway_namespace),
        lambda value: bool((value.get("status") or {}).get("addresses")),
        timeout,
        f"address for gateway/{gateway_name}",
    )
    address = str(gateway["status"]["addresses"][0].get("value") or "").strip()
    if not address:
        raise DeploymentError(f"gateway/{gateway_name} has an empty address")

    listeners = (gateway.get("spec") or {}).get("listeners") or []
    section_name = str(parent.get("sectionName") or "")
    listener = next(
        (
            item
            for item in listeners
            if not section_name or item.get("name") == section_name
        ),
        None,
    )
    if not listener:
        raise DeploymentError(f"gateway/{gateway_name} has no matching listener")
    protocol = str(listener.get("protocol") or "HTTP").upper()
    scheme = "https" if protocol in {"HTTPS", "TLS"} else "http"
    port = int(listener.get("port") or (443 if scheme == "https" else 80))
    if scheme == "https":
        return NetworkAddress(scheme, resources.hostname, port)
    return NetworkAddress(scheme, address, port, resources.hostname)


def _find_address(
    resources: ForetokenDeployment, kubectl: Kubectl, timeout: float
) -> NetworkAddress:
    """Find the public LoadBalancer or Gateway address used by a benchmark."""
    service = kubectl.get("service", resources.frontend, resources.namespace)
    if (service.get("spec") or {}).get("type") == "LoadBalancer":
        return _load_balancer_address(service, resources, kubectl, timeout)
    return _gateway_address(resources, kubectl, timeout)


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
    resources: ForetokenDeployment,
    kubectl: Kubectl,
    timeout: str,
    *,
    requested_model: str,
    api_key: str,
) -> BenchmarkEndpoint:
    """Find the endpoint and model for rendered Foretoken resources."""
    wait_seconds = timeout_seconds(timeout)
    model = _select_model(resources.models.values(), requested_model)
    wait_for_resources(resources.service_refs(), kubectl, timeout)
    address = _find_address(resources, kubectl, wait_seconds)
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
