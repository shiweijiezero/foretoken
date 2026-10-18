# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve public frontend addresses from their controller-owned Services and Gateways."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from foretoken.arguments import EndpointCommand
from foretoken.command_execution import CommandExecution
from foretoken.deployment.manifest import ForetokenDeployment, load_deployment
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object


@dataclass(frozen=True)
class FrontendEndpoint:
    """Public frontend URL and optional HTTP routing hostname."""

    url: str
    routing_host: str = ""


def _endpoint_url(scheme: str, host: str, port: int) -> str:
    """Format a network endpoint while preserving IPv6 address syntax."""
    default_port = 443 if scheme == "https" else 80
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _load_balancer_endpoint(
    service: dict[str, Any],
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    deadline: float,
) -> FrontendEndpoint:
    """Resolve the frontend's controller-owned LoadBalancer Service endpoint."""

    def has_ingress(value: dict[str, Any]) -> bool:
        ingress = ((value.get("status") or {}).get("loadBalancer") or {}).get(
            "ingress"
        )
        return bool(ingress)

    if not has_ingress(service):
        service = wait_for_object(
            lambda: kubectl.get(
                "service", deployment.frontend, deployment.namespace
            ),
            has_ingress,
            deadline,
            f"LoadBalancer address for service/{deployment.frontend} in namespace "
            f"{deployment.namespace}; run 'kubectl describe service/{deployment.frontend} "
            f"--namespace {deployment.namespace}' and verify the cluster LoadBalancer "
            "implementation, or configure loadBalancer.managedAddresses for "
            "'foretoken install --values PATH'",
        )
    ingress = service["status"]["loadBalancer"]["ingress"][0]
    address = str(ingress.get("ip") or ingress.get("hostname") or "").strip()
    if not address:
        raise DeploymentError(
            f"service/{deployment.frontend} has an empty LoadBalancer address"
        )
    ports = (service.get("spec") or {}).get("ports") or []
    http_port = next(
        (item for item in ports if item.get("name") == "http"),
        ports[0] if ports else None,
    )
    if not http_port or not http_port.get("port"):
        raise DeploymentError(
            f"service/{deployment.frontend} has no public HTTP port"
        )
    return FrontendEndpoint(
        _endpoint_url("http", address, int(http_port["port"]))
    )


def resolve_gateway_access(
    kubectl: Kubectl, name: str, namespace: str, section_name: str, timeout: str,
) -> dict[str, Any]:
    """Resolve a public Gateway listener for namespace preparation and endpoint consumers."""
    gateway = wait_for_object(
        lambda: kubectl.get_if_exists("gateway", name, namespace) or {},
        lambda value: bool((value.get("status") or {}).get("addresses")),
        time.monotonic() + timeout_seconds(timeout),
        f"address for gateway/{name} in namespace {namespace}; inspect its Gateway controller Service and LoadBalancer",
    )
    address = str(gateway["status"]["addresses"][0].get("value") or "").strip()
    if not address:
        raise DeploymentError(f"gateway/{name} has an empty address")
    listeners = tuple(
        item for item in (gateway.get("spec") or {}).get("listeners") or []
        if not section_name or item.get("name") == section_name
    )
    if len(listeners) != 1:
        if not section_name and len(listeners) > 1:
            raise DeploymentError(
                f"gateway/{name} has multiple listeners; reinstall with --gateway-section-name to select one"
            )
        raise DeploymentError(f"gateway/{name} has no matching listener")
    listener = listeners[0]
    protocol = str(listener.get("protocol") or "HTTP").upper()
    scheme = "https" if protocol in {"HTTPS", "TLS"} else "http"
    return {
        "name": name, "namespace": namespace, "sectionName": section_name,
        "address": address, "scheme": scheme,
        "port": int(listener.get("port") or (443 if scheme == "https" else 80)),
    }


def _gateway_endpoint(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    deadline: float,
) -> FrontendEndpoint:
    """Resolve the Gateway selected by the frontend's controller-owned HTTPRoute."""
    if not deployment.hostname:
        raise DeploymentError("gateway frontend does not declare spec.hostname")
    def has_gateway_parent(value: dict[str, Any]) -> bool:
        return any(
            item.get("kind", "Gateway") == "Gateway"
            and item.get("group", "gateway.networking.k8s.io")
            == "gateway.networking.k8s.io"
            and item.get("name")
            for item in (value.get("spec") or {}).get("parentRefs") or []
        )

    route = wait_for_object(
        lambda: kubectl.get_if_exists(
            "httproute", deployment.frontend, deployment.namespace
        )
        or {},
        has_gateway_parent,
        deadline,
        f"HTTPRoute for frontendservice/{deployment.frontend}",
    )
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
        raise DeploymentError(f"httproute/{deployment.frontend} has no Gateway parent")

    from foretoken.deployment.namespace_access import NamespaceAccess

    gateway_name = str(parent["name"])
    gateway_namespace = str(parent.get("namespace") or deployment.namespace)
    section_name = str(parent.get("sectionName") or "")
    access = NamespaceAccess.read(kubectl, deployment.namespace)
    if access is not None:
        published = access.gateway
        if not published or (published["namespace"], published["name"], published["sectionName"]) != (
            gateway_namespace, gateway_name, section_name,
        ):
            raise DeploymentError(
                f"Gateway access for namespace {deployment.namespace} must be refreshed by its administrator"
            )
    else:
        published = resolve_gateway_access(
            kubectl, gateway_name, gateway_namespace, section_name,
            f"{max(1, int(deadline - time.monotonic()))}s",
        )
    address, scheme, port = published["address"], published["scheme"], published["port"]
    if scheme == "https":
        return FrontendEndpoint(
            _endpoint_url(scheme, deployment.hostname, port)
        )
    return FrontendEndpoint(
        _endpoint_url(scheme, address, port), deployment.hostname
    )


def resolve_frontend_endpoint(
    deployment: ForetokenDeployment, kubectl: Kubectl, timeout: str
) -> FrontendEndpoint:
    """Wait for and return the deployment's public frontend network endpoint."""
    with kubectl.until(time.monotonic() + timeout_seconds(timeout)) as deadline:
        service = wait_for_object(
            lambda: kubectl.get_if_exists(
                "service", deployment.frontend, deployment.namespace
            )
            or {},
            bool,
            deadline,
            f"service/{deployment.frontend}",
        )
        if (service.get("spec") or {}).get("type") == "LoadBalancer":
            return _load_balancer_endpoint(service, deployment, kubectl, deadline)
        return _gateway_endpoint(deployment, kubectl, deadline)


def print_endpoint(command: EndpointCommand) -> None:
    """Print the CLI-selected frontend URL or routing host after a bounded endpoint wait."""
    with CommandExecution(timeout_seconds(command.timeout)):
        kubectl = Kubectl()
        deployment = load_deployment(command.kustomize_path, kubectl)
        print("Waiting for the frontend endpoint", file=sys.stderr, flush=True)
        endpoint = resolve_frontend_endpoint(deployment, kubectl, command.timeout)
        if command.host:
            print(endpoint.routing_host or urlsplit(endpoint.url).netloc)
        else:
            print(endpoint.url)
