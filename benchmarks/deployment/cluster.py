# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Kubernetes readiness and public address discovery."""

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

from benchmarks.deployment.manifest import DeploymentError, DeploymentResources


@dataclass(frozen=True)
class NetworkAddress:
    """Public network address and optional HTTP routing hostname."""

    scheme: str
    host: str
    port: int
    routing_host: str = ""


class Kubectl:
    """Run kubectl commands while preserving their diagnostics."""

    def __init__(self) -> None:
        if shutil.which("kubectl") is None:
            raise DeploymentError(
                "kubectl is required to inspect a Foretoken deployment"
            )

    def run(
        self, args: Iterable[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
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
        return self.run(["kustomize", str(path)]).stdout

    def apply_kustomize(self, path: Path) -> None:
        self.run(["apply", "--server-side", "-k", str(path)])

    def exists(self, kind: str, name: str, namespace: str = "") -> bool:
        args = ["get", kind, name, "--ignore-not-found", "-o", "name"]
        if namespace:
            args.extend(["--namespace", namespace])
        return bool(self.run(args).stdout.strip())

    def delete_yaml(self, rendered: str, timeout: str) -> None:
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

    def get(self, kind: str, name: str, namespace: str = "") -> dict[str, Any]:
        args = ["get", kind, name]
        if namespace:
            args.extend(["--namespace", namespace])
        output = self.run([*args, "-o", "json"]).stdout
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DeploymentError("kubectl returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DeploymentError("kubectl returned an unexpected JSON value")
        return value

    def wait_ready(self, resources: list[str], namespace: str, timeout: str) -> None:
        args = ["wait", "--for=condition=Ready", f"--timeout={timeout}"]
        if namespace:
            args.extend(["--namespace", namespace])
        self.run([*args, *resources])


def timeout_seconds(value: str) -> float:
    """Convert a Kubernetes-style duration used by deployment waits."""
    parts = re.findall(r"(\d+)([smh])", value)
    if not parts or "".join(f"{amount}{unit}" for amount, unit in parts) != value:
        raise DeploymentError(
            "--wait-timeout must use Kubernetes duration syntax, such as 15m"
        )
    multipliers = {"s": 1, "m": 60, "h": 3600}
    return float(sum(int(amount) * multipliers[unit] for amount, unit in parts))


def wait_for_deployment(
    resources: DeploymentResources, kubectl: Kubectl, timeout: str
) -> None:
    """Wait for the deployed model and frontend services to be Ready."""
    service_names = [f"modelservice/{name}" for name in resources.models]
    service_names.append(f"frontendservice/{resources.frontend}")
    kubectl.wait_ready(service_names, resources.namespace, timeout)


def _wait_for_object(
    fetch: Callable[[], dict[str, Any]],
    ready: Callable[[dict[str, Any]], bool],
    timeout: float,
    description: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fetch()
        if ready(value):
            return value
        time.sleep(2)
    raise DeploymentError(f"timed out waiting for {description}")


def _load_balancer_address(
    service: dict[str, Any],
    resources: DeploymentResources,
    kubectl: Kubectl,
    timeout: float,
) -> NetworkAddress:
    def has_ingress(value: dict[str, Any]) -> bool:
        ingress = ((value.get("status") or {}).get("loadBalancer") or {}).get("ingress")
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
    resources: DeploymentResources,
    kubectl: Kubectl,
    timeout: float,
) -> NetworkAddress:
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


def find_address(
    resources: DeploymentResources, kubectl: Kubectl, timeout: float
) -> NetworkAddress:
    """Find the public LoadBalancer or Gateway network address."""
    service = kubectl.get("service", resources.frontend, resources.namespace)
    if (service.get("spec") or {}).get("type") == "LoadBalancer":
        return _load_balancer_address(service, resources, kubectl, timeout)
    return _gateway_address(resources, kubectl, timeout)
