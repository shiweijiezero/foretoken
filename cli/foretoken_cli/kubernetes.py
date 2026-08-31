# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Kubernetes deployment, readiness, and public address discovery."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken_cli.manifest import (
    DeploymentError,
    ForetokenDeployment,
    ResourceRef,
    deployment_path,
    parse_deployment,
)


@dataclass(frozen=True)
class FrontendEndpoint:
    """Public frontend URL and optional HTTP routing hostname."""

    url: str
    routing_host: str = ""


@dataclass(frozen=True)
class ResourceProgress:
    """Current-generation readiness reported by one Foretoken service."""

    resource: ResourceRef
    state: str
    reason: str
    message: str
    ready: bool

    @property
    def detail(self) -> str:
        """Return the most useful concise status explanation."""
        return self.message or self.reason


class Kubectl:
    """Run kubectl commands using the caller's active Kubernetes context."""

    def __init__(self) -> None:
        if shutil.which("kubectl") is None:
            raise DeploymentError(
                "kubectl is required to deploy or inspect Foretoken services"
            )

    def run(
        self, args: Iterable[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Execute kubectl and preserve its diagnostic output on failure."""
        command = ["kubectl", *args]
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise DeploymentError(f"{' '.join(command)} failed: {detail}")
        return completed

    def kustomize(self, path: Path) -> str:
        """Render a Kustomize root through the installed kubectl."""
        return self.run(["kustomize", str(path)]).stdout

    def apply(self, rendered: str) -> None:
        """Server-side apply one rendered payload and leave resources user-owned."""
        self.run(
            ["apply", "--server-side", "--filename=-"],
            input_text=rendered,
        )

    def delete(self, rendered: str, timeout: str) -> None:
        """Delete one rendered payload and wait for its resources to terminate."""
        self.run(
            [
                "delete",
                "--filename=-",
                "--ignore-not-found",
                "--wait=true",
                f"--timeout={timeout}",
            ],
            input_text=rendered,
        )

    def exists(self, kind: str, name: str, namespace: str = "") -> bool:
        """Return whether a named Kubernetes resource currently exists."""
        args = ["get", kind, name, "--ignore-not-found", "-o", "name"]
        if namespace:
            args.extend(["--namespace", namespace])
        return bool(self.run(args).stdout.strip())

    def get(self, kind: str, name: str, namespace: str = "") -> dict[str, Any]:
        """Return one Kubernetes object as decoded JSON."""
        args = ["get", kind, name]
        if namespace:
            args.extend(["--namespace", namespace])
        return _decode_object(self.run([*args, "-o", "json"]).stdout)

    def get_if_exists(
        self, kind: str, name: str, namespace: str = ""
    ) -> dict[str, Any] | None:
        """Return one Kubernetes object, or None while it does not exist."""
        args = ["get", kind, name, "--ignore-not-found"]
        if namespace:
            args.extend(["--namespace", namespace])
        output = self.run([*args, "-o", "json"]).stdout.strip()
        return _decode_object(output) if output else None

    def get_resources(
        self, resources: tuple[ResourceRef, ...]
    ) -> tuple[dict[str, Any], ...]:
        """Return named resources from one namespace with a single kubectl call."""
        namespaces = {resource.namespace for resource in resources}
        if len(namespaces) != 1:
            raise DeploymentError("selected resources must share one namespace")
        args = [
            "get",
            *(f"{resource.kind.lower()}/{resource.name}" for resource in resources),
        ]
        namespace = next(iter(namespaces))
        if namespace:
            args.extend(["--namespace", namespace])
        value = _decode_object(self.run([*args, "-o", "json"]).stdout)
        items = value.get("items") or []
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise DeploymentError("kubectl returned an unexpected resource list")
        return tuple(items)

    def list_resources(
        self, kinds: Iterable[str], namespace: str
    ) -> tuple[dict[str, Any], ...]:
        """Return the selected resource kinds in one namespace."""
        args = [
            "get",
            ",".join(kinds),
            "--namespace",
            namespace,
            "-o",
            "json",
        ]
        value = _decode_object(self.run(args).stdout)
        items = value.get("items") or []
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise DeploymentError("kubectl returned an unexpected resource list")
        return tuple(items)


def _decode_object(output: str) -> dict[str, Any]:
    """Decode the Kubernetes object contract returned by kubectl JSON output."""
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("kubectl returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError("kubectl returned an unexpected JSON value")
    return value


def load_deployment(path_value: str, kubectl: Kubectl) -> ForetokenDeployment:
    """Render and identify one Kustomize deployment for CLI or benchmark use."""
    path = deployment_path(path_value)
    return parse_deployment(path, kubectl.kustomize(path))


def timeout_seconds(value: str) -> float:
    """Convert a Kubernetes-style duration used by deployment waits."""
    parts = re.findall(r"(\d+)([smh])", value)
    if not parts or "".join(f"{amount}{unit}" for amount, unit in parts) != value:
        raise DeploymentError(
            "--timeout must use Kubernetes duration syntax, such as 10m or 1h30m"
        )
    multipliers = {"s": 1, "m": 60, "h": 3600}
    return float(sum(int(amount) * multipliers[unit] for amount, unit in parts))


def resource_progress(
    resource: ResourceRef, value: dict[str, Any]
) -> ResourceProgress:
    """Interpret one service's Ready condition without accepting stale generations."""
    metadata = value.get("metadata") or {}
    status = value.get("status") or {}
    if metadata.get("deletionTimestamp"):
        return ResourceProgress(
            resource, "Terminating", "Deleting", "Resource is being deleted", False
        )

    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)
    conditions = status.get("conditions") or []
    ready_condition = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "Ready"
        ),
        None,
    )
    if ready_condition is None:
        return ResourceProgress(
            resource,
            "Progressing",
            "Reconciling",
            "Waiting for controller status",
            False,
        )

    reason = str(ready_condition.get("reason") or "Reconciling")
    message = str(ready_condition.get("message") or "")
    condition_generation = int(ready_condition.get("observedGeneration") or 0)
    if observed_generation != generation or condition_generation != generation:
        return ResourceProgress(
            resource,
            "Progressing",
            "Updating",
            "Waiting for the current configuration to be reconciled",
            False,
        )

    condition_status = str(ready_condition.get("status") or "Unknown")
    if condition_status == "True":
        return ResourceProgress(resource, "Ready", reason, message, True)
    if condition_status == "False" and reason == "InvalidIntent":
        return ResourceProgress(resource, "Failed", reason, message, False)
    return ResourceProgress(resource, "Progressing", reason, message, False)


def namespace_progress(
    namespace: str, kubectl: Kubectl
) -> tuple[ResourceProgress, ...]:
    """Read every user-facing Foretoken service in one namespace."""
    progress: list[ResourceProgress] = []
    values = kubectl.list_resources(
        ("frontendservice", "modelservice"), namespace
    )
    for value in values:
        kind = str(value.get("kind") or "")
        name = str((value.get("metadata") or {}).get("name") or "").strip()
        if kind in {"FrontendService", "ModelService"} and name:
            progress.append(
                resource_progress(ResourceRef(kind, name, namespace), value)
            )
    if not progress:
        raise DeploymentError(
            f"no FrontendService or ModelService resources found in namespace {namespace}"
        )
    return tuple(progress)


def read_progress(
    resources: Iterable[ResourceRef], kubectl: Kubectl
) -> tuple[ResourceProgress, ...]:
    """Read named service readiness with one Kubernetes request."""
    selected = tuple(resources)
    values = kubectl.get_resources(selected)
    by_identity = {
        (
            str(value.get("kind") or ""),
            str((value.get("metadata") or {}).get("name") or ""),
        ): value
        for value in values
    }
    try:
        return tuple(
            resource_progress(resource, by_identity[(resource.kind, resource.name)])
            for resource in selected
        )
    except KeyError as exc:
        raise DeploymentError("kubectl omitted a selected Foretoken service") from exc


def wait_for_resources(
    resources: tuple[ResourceRef, ...],
    kubectl: Kubectl,
    timeout: str,
    *,
    report: Callable[[float, ResourceProgress], None] | None = None,
) -> tuple[ResourceProgress, ...]:
    """Wait until every service reports Ready for its current generation."""
    timeout_value = timeout_seconds(timeout)
    started = time.monotonic()
    deadline = started + timeout_value
    previous: dict[ResourceRef, tuple[str, str, str]] = {}
    latest: tuple[ResourceProgress, ...] = ()

    while True:
        latest = read_progress(resources, kubectl)
        elapsed = time.monotonic() - started
        if report is not None:
            for progress in latest:
                signature = (progress.state, progress.reason, progress.message)
                if previous.get(progress.resource) != signature:
                    report(elapsed, progress)
                    previous[progress.resource] = signature
        if all(progress.ready for progress in latest):
            return latest
        if any(progress.state == "Failed" for progress in latest):
            failed = next(progress for progress in latest if progress.state == "Failed")
            raise DeploymentError(
                f"{failed.resource.display_name} failed: {failed.detail or failed.reason}"
            )
        if time.monotonic() >= deadline:
            pending = "; ".join(
                f"{item.resource.display_name}: {item.detail or item.state}"
                for item in latest
                if not item.ready
            )
            raise DeploymentError(
                f"timed out after {timeout} waiting for Foretoken services: {pending}"
            )
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def _wait_for_object(
    fetch: Callable[[], dict[str, Any]],
    ready: Callable[[dict[str, Any]], bool],
    deadline: float,
    description: str,
) -> dict[str, Any]:
    """Poll one endpoint dependency before the command deadline."""
    while time.monotonic() < deadline:
        value = fetch()
        if ready(value):
            return value
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise DeploymentError(f"timed out waiting for {description}")


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
        service = _wait_for_object(
            lambda: kubectl.get(
                "service", deployment.frontend, deployment.namespace
            ),
            has_ingress,
            deadline,
            f"LoadBalancer address for service/{deployment.frontend}",
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

    route = _wait_for_object(
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

    gateway_name = str(parent["name"])
    gateway_namespace = str(parent.get("namespace") or deployment.namespace)
    gateway = _wait_for_object(
        lambda: kubectl.get_if_exists(
            "gateway", gateway_name, gateway_namespace
        )
        or {},
        lambda value: bool((value.get("status") or {}).get("addresses")),
        deadline,
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
    deadline = time.monotonic() + timeout_seconds(timeout)
    service = _wait_for_object(
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
