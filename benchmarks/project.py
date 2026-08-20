# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Public benchmark target discovery from a Kustomize project."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml


class ProjectError(RuntimeError):
    """A project cannot be rendered, deployed, or reached."""


@dataclass(frozen=True)
class ProjectResources:
    """Public serving resources rendered from one project."""

    path: Path
    namespace: str
    frontend: str
    hostname: str
    models: dict[str, str]

    def select_model(self, requested: str = "") -> str:
        available = sorted(set(self.models.values()))
        if requested:
            if requested not in available:
                raise ProjectError(
                    f"model {requested!r} is not declared by {self.path}; "
                    f"available models: {', '.join(available)}"
                )
            return requested
        if len(available) != 1:
            raise ProjectError(
                "the project declares multiple models; pass --model with one of: "
                + ", ".join(available)
            )
        return available[0]


@dataclass(frozen=True)
class ProjectEndpoint:
    """Public endpoint and models exposed by a deployed project."""

    url: str
    models: tuple[str, ...]
    headers: dict[str, str]


class Kubectl:
    """Small subprocess boundary that preserves kubectl diagnostics."""

    def __init__(self) -> None:
        if shutil.which("kubectl") is None:
            raise ProjectError("kubectl is required to run a Foretoken project")

    def run(self, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
        command = ["kubectl", *args]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ProjectError(f"{' '.join(command)} failed: {detail}")
        return completed

    def json(self, args: Iterable[str]) -> dict[str, Any]:
        output = self.run([*args, "-o", "json"]).stdout
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProjectError("kubectl returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProjectError("kubectl returned an unexpected JSON value")
        return value


# Resource names, namespaces, and public model IDs come from the same
# Kustomize output users deploy through the documented Kubernetes workflow.
def load_project(path_value: str, kubectl: Kubectl) -> ProjectResources:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise ProjectError(f"project directory not found: {path}")
    if not any(
        (path / name).is_file()
        for name in ("kustomization.yaml", "kustomization.yml", "Kustomization")
    ):
        raise ProjectError(f"project is not a Kustomize root: {path}")

    rendered = kubectl.run(["kustomize", str(path)]).stdout
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise ProjectError(f"project rendered invalid YAML: {exc}") from exc
    if not documents:
        raise ProjectError(f"project rendered no Kubernetes resources: {path}")

    frontends: list[dict[str, Any]] = []
    models: dict[str, str] = {}
    namespaces: set[str] = set()
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise ProjectError(f"rendered document {index} is not a Kubernetes object")
        metadata = document.get("metadata") or {}
        namespace = str(metadata.get("namespace") or "").strip()
        kind = document.get("kind")
        if kind in {"FrontendService", "ModelService"}:
            namespaces.add(namespace)
        if kind == "FrontendService":
            frontends.append(document)
        elif kind == "ModelService":
            name = str(metadata.get("name") or "").strip()
            model = str((document.get("spec") or {}).get("model") or "").strip()
            if not name or not model:
                raise ProjectError(
                    "each ModelService requires metadata.name and spec.model"
                )
            models[name] = model

    if len(frontends) != 1:
        raise ProjectError("a project must render exactly one FrontendService")
    if not models:
        raise ProjectError("a project must render at least one ModelService")
    if len(namespaces) != 1:
        raise ProjectError(
            "FrontendService and ModelService resources must share one namespace"
        )

    frontend = frontends[0]
    metadata = frontend.get("metadata") or {}
    name = str(metadata.get("name") or "").strip()
    if not name:
        raise ProjectError("FrontendService requires metadata.name")
    hostname = str((frontend.get("spec") or {}).get("hostname") or "").strip()
    return ProjectResources(
        path=path,
        namespace=next(iter(namespaces)),
        frontend=name,
        hostname=hostname,
        models=models,
    )


def _timeout_seconds(value: str) -> float:
    parts = re.findall(r"(\d+)([smh])", value)
    if not parts or "".join(f"{amount}{unit}" for amount, unit in parts) != value:
        raise ProjectError(
            "--wait-timeout must use Kubernetes duration syntax, such as 15m"
        )
    multipliers = {"s": 1, "m": 60, "h": 3600}
    return float(sum(int(amount) * multipliers[unit] for amount, unit in parts))


def _resource_name(kind: str, name: str) -> str:
    return f"{kind.lower()}/{name}"


def _namespace_args(namespace: str) -> list[str]:
    return ["--namespace", namespace] if namespace else []


def wait_for_project(
    resources: ProjectResources, kubectl: Kubectl, timeout: str
) -> None:
    """Wait for the deployed project's serving APIs to be Ready."""
    _timeout_seconds(timeout)
    model_resources = [
        _resource_name("modelservice", name) for name in resources.models
    ]
    kubectl.run(
        [
            "wait",
            "--for=condition=Ready",
            f"--timeout={timeout}",
            *_namespace_args(resources.namespace),
            *model_resources,
        ]
    )
    kubectl.run(
        [
            "wait",
            "--for=condition=Ready",
            f"--timeout={timeout}",
            *_namespace_args(resources.namespace),
            _resource_name("frontendservice", resources.frontend),
        ]
    )


def _host_for_url(value: str) -> str:
    return f"[{value}]" if ":" in value and not value.startswith("[") else value


def _url(scheme: str, host: str, port: int) -> str:
    default_port = 443 if scheme == "https" else 80
    authority = _host_for_url(host)
    if port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}/v1/chat/completions"


def _wait_for_json(
    kubectl: Kubectl,
    args: list[str],
    timeout_seconds: float,
    ready: Any,
    description: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = kubectl.json(args)
        if ready(last):
            return last
        time.sleep(2)
    raise ProjectError(f"timed out waiting for {description}")


def _load_balancer_target(
    service: dict[str, Any],
    resources: ProjectResources,
    kubectl: Kubectl,
    timeout_seconds: float,
) -> tuple[str, dict[str, str]]:
    def has_ingress(value: dict[str, Any]) -> bool:
        ingress = ((value.get("status") or {}).get("loadBalancer") or {}).get("ingress")
        return bool(ingress)

    if not has_ingress(service):
        service = _wait_for_json(
            kubectl,
            [
                "get",
                "service",
                resources.frontend,
                *_namespace_args(resources.namespace),
            ],
            timeout_seconds,
            has_ingress,
            f"LoadBalancer address for service/{resources.frontend}",
        )
    ingress = service["status"]["loadBalancer"]["ingress"][0]
    address = str(ingress.get("ip") or ingress.get("hostname") or "").strip()
    if not address:
        raise ProjectError(
            f"service/{resources.frontend} has an empty LoadBalancer address"
        )
    ports = (service.get("spec") or {}).get("ports") or []
    http_port = next(
        (item for item in ports if item.get("name") == "http"),
        ports[0] if ports else None,
    )
    if not http_port or not http_port.get("port"):
        raise ProjectError(f"service/{resources.frontend} has no public HTTP port")
    return _url("http", address, int(http_port["port"])), {}


def _gateway_target(
    resources: ProjectResources,
    kubectl: Kubectl,
    timeout_seconds: float,
) -> tuple[str, dict[str, str]]:
    if not resources.hostname:
        raise ProjectError("gateway frontend does not declare spec.hostname")
    route = kubectl.json(
        ["get", "httproute", resources.frontend, *_namespace_args(resources.namespace)]
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
        raise ProjectError(f"httproute/{resources.frontend} has no Gateway parent")
    gateway_namespace = str(parent.get("namespace") or resources.namespace)
    gateway_name = str(parent["name"])

    def has_address(value: dict[str, Any]) -> bool:
        return bool((value.get("status") or {}).get("addresses"))

    gateway = _wait_for_json(
        kubectl,
        ["get", "gateway", gateway_name, *_namespace_args(gateway_namespace)],
        timeout_seconds,
        has_address,
        f"address for gateway/{gateway_name}",
    )
    address = str(gateway["status"]["addresses"][0].get("value") or "").strip()
    if not address:
        raise ProjectError(f"gateway/{gateway_name} has an empty address")

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
        raise ProjectError(f"gateway/{gateway_name} has no matching listener")
    protocol = str(listener.get("protocol") or "HTTP").upper()
    scheme = "https" if protocol in {"HTTPS", "TLS"} else "http"
    port = int(listener.get("port") or (443 if scheme == "https" else 80))
    if scheme == "https":
        return _url(scheme, resources.hostname, port), {}
    return _url(scheme, address, port), {"Host": resources.hostname}


def project_endpoint(
    resources: ProjectResources,
    kubectl: Kubectl,
    timeout: str,
) -> tuple[str, dict[str, str]]:
    """Resolve the public endpoint without exposing controller-owned internals."""
    timeout_seconds = _timeout_seconds(timeout)
    service = kubectl.json(
        ["get", "service", resources.frontend, *_namespace_args(resources.namespace)]
    )
    if (service.get("spec") or {}).get("type") == "LoadBalancer":
        return _load_balancer_target(service, resources, kubectl, timeout_seconds)
    return _gateway_target(resources, kubectl, timeout_seconds)


def _root_url(chat_url: str) -> str:
    parsed = urlsplit(chat_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def wait_for_endpoint(
    url: str,
    headers: dict[str, str],
    timeout: str,
    expected_models: Iterable[str],
    api_key: str = "",
) -> tuple[str, ...]:
    """Wait for the public frontend and confirm its advertised model IDs."""
    expected = set(expected_models)
    deadline = time.monotonic() + _timeout_seconds(timeout)
    request_headers = dict(headers)
    if api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    root = _root_url(url)
    last_error = "frontend has not responded"
    with httpx.Client(
        headers=request_headers, timeout=5.0, follow_redirects=True
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{root}/v1/models")
                response.raise_for_status()
                payload = response.json()
                models = tuple(
                    sorted(
                        str(item["id"])
                        for item in payload.get("data", [])
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
    raise ProjectError(f"frontend did not become ready: {last_error}")


def benchmark_project(
    project: str,
    timeout: str,
    *,
    requested_model: str = "",
    api_key: str = "",
) -> tuple[ProjectResources, ProjectEndpoint, str]:
    """Find a deployed project's endpoint and benchmark model."""
    kubectl = Kubectl()
    resources = load_project(project, kubectl)
    selected_model = resources.select_model(requested_model)
    wait_for_project(resources, kubectl, timeout)
    url, headers = project_endpoint(resources, kubectl, timeout)
    models = wait_for_endpoint(
        url,
        headers,
        timeout,
        resources.models.values(),
        api_key=api_key,
    )
    endpoint = ProjectEndpoint(url, models, headers)
    if selected_model not in endpoint.models:
        raise ProjectError(
            f"model {selected_model!r} is not advertised by the frontend; "
            f"available models: {', '.join(endpoint.models)}"
        )
    return resources, endpoint, selected_model
