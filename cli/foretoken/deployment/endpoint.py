# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve public frontend addresses from their controller-owned Services and Gateways."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from foretoken.arguments import EndpointCommand
from foretoken.deployment.instances import DeploymentInstance, resolve_deployment_target
from foretoken.deployment.manifest import DEPLOYMENT_UID_LABEL, ForetokenDeployment
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds, wait_for_object
from foretoken.processes.execution import CommandExecution


@dataclass(frozen=True)
class FrontendEndpoint:
    """Public frontend URL and optional HTTP routing hostname."""

    url: str
    routing_host: str = ""


def print_endpoint(command: EndpointCommand) -> None:
    """Print the CLI-selected frontend URL or routing host after a bounded endpoint wait."""
    with CommandExecution(timeout_seconds(command.timeout)):
        kubectl = Kubectl()
        deployment = resolve_deployment_target(command.target, kubectl, name=command.name, allow_missing=True)
        logger.info("Waiting for the frontend endpoint")
        endpoint = resolve_frontend_endpoint(deployment, kubectl, command.timeout)
        if command.host:
            print(endpoint.routing_host or urlsplit(endpoint.url).netloc)
        else:
            print(endpoint.url)


def resolve_frontend_endpoint(
    deployment: ForetokenDeployment, kubectl: Kubectl, timeout: str
) -> FrontendEndpoint:
    """Wait for and return the deployment's public frontend network endpoint."""
    if not deployment.frontend:
        raise DeploymentError(f"Deployment instance {deployment.name!r} retains storage but has no frontend")
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
        if deployment.uid and service["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != deployment.uid:
            raise DeploymentError(f"service/{deployment.frontend} does not belong to deployment instance {deployment.name!r}")
        if (service.get("spec") or {}).get("type") == "LoadBalancer":
            return _load_balancer_endpoint(service, deployment, kubectl, deadline)
        return _gateway_endpoint(deployment, kubectl, deadline)


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
    if deployment.uid and service["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != deployment.uid:
        raise DeploymentError(f"service/{deployment.frontend} does not belong to deployment instance {deployment.name!r}")
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


def _gateway_endpoint(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    deadline: float,
) -> FrontendEndpoint:
    """Resolve the Gateway selected by the frontend's controller-owned HTTPRoute."""
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
    if deployment.uid and route["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != deployment.uid:
        raise DeploymentError(f"HTTPRoute/{deployment.frontend} does not belong to deployment instance {deployment.name!r}")
    hostnames = (route.get("spec") or {}).get("hostnames") or []
    if len(hostnames) != 1:
        raise DeploymentError(f"HTTPRoute/{deployment.frontend} has no single published routing hostname")
    hostname = hostnames[0]
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

    gateway_name = str(parent["name"])
    gateway_namespace = str(parent.get("namespace") or deployment.namespace)
    section_name = str(parent.get("sectionName") or "")
    instance = DeploymentInstance(kubectl, deployment.namespace, deployment.name)
    instance.uid = deployment.uid
    published = instance.access()["gateway"]
    if not published or (published["namespace"], published["name"], published["sectionName"]) != (
        gateway_namespace, gateway_name, section_name,
    ):
        raise DeploymentError(
            f"Gateway access for deployment instance {deployment.name!r} is not current; redeploy the instance"
        )
    address, scheme, port = published["address"], published["scheme"], published["port"]
    if scheme == "https":
        if not deployment.hostname:
            raise DeploymentError("HTTPS Gateway access requires spec.hostname matching the listener certificate")
        return FrontendEndpoint(_endpoint_url(scheme, hostname, port))
    return FrontendEndpoint(_endpoint_url(scheme, address, port), hostname)
