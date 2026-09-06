# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render and identify Foretoken serving resources from Kustomize."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class DeploymentError(RuntimeError):
    """A Foretoken deployment cannot be applied or inspected."""


@dataclass(frozen=True)
class ResourceRef:
    """A namespaced Foretoken resource selected for status inspection."""

    kind: str
    name: str
    namespace: str

    @property
    def display_name(self) -> str:
        """Return the user-facing kind and resource name."""
        return f"{self.kind}/{self.name}"


@dataclass(frozen=True)
class ForetokenDeployment:
    """Rendered Kustomize payload and its user-facing Foretoken services."""

    path: Path
    rendered: str
    namespace: str
    frontend: str
    hostname: str
    models: dict[str, str]
    objects: tuple[dict[str, Any], ...]

    def service_refs(self) -> tuple[ResourceRef, ...]:
        """Return the services whose current generation defines deployment readiness."""
        return (
            ResourceRef("FrontendService", self.frontend, self.namespace),
            *(
                ResourceRef("ModelService", name, self.namespace)
                for name in self.models
            ),
        )


def deployment_path(path_value: str) -> Path:
    """Return a validated Kustomize root supplied by a CLI caller."""
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise DeploymentError(f"deployment directory not found: {path}")
    names = ("kustomization.yaml", "kustomization.yml", "Kustomization")
    if not any((path / name).is_file() for name in names):
        raise DeploymentError(f"deployment directory is not a Kustomize root: {path}")
    return path


def parse_deployment(path: Path, rendered: str) -> ForetokenDeployment:
    """Extract the public frontend and models from rendered Kubernetes YAML."""
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise DeploymentError(f"deployment rendered invalid YAML: {exc}") from exc
    if not documents:
        raise DeploymentError(f"deployment rendered no Kubernetes resources: {path}")

    frontends: list[dict[str, Any]] = []
    models: dict[str, str] = {}
    namespaces: set[str] = set()
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise DeploymentError(
                f"rendered document {index} is not a Kubernetes object"
            )
        metadata = document.get("metadata") or {}
        api_version = str(document.get("apiVersion") or "")
        kind = document.get("kind")
        if kind not in {"FrontendService", "ModelService"}:
            continue
        if not api_version.startswith("inference.foretoken.io/"):
            continue

        namespaces.add(str(metadata.get("namespace") or "").strip())
        if kind == "FrontendService":
            frontends.append(document)
            continue

        name = str(metadata.get("name") or "").strip()
        model = str((document.get("spec") or {}).get("model") or "").strip()
        if not name or not model:
            raise DeploymentError(
                "each ModelService requires metadata.name and spec.model"
            )
        models[name] = model

    if len(frontends) != 1:
        raise DeploymentError(
            "a deployment must render exactly one Foretoken FrontendService"
        )
    if not models:
        raise DeploymentError(
            "a deployment must render at least one Foretoken ModelService"
        )
    if len(namespaces) != 1:
        raise DeploymentError(
            "FrontendService and ModelService resources must share one namespace"
        )

    frontend = frontends[0]
    metadata = frontend.get("metadata") or {}
    name = str(metadata.get("name") or "").strip()
    if not name:
        raise DeploymentError("FrontendService requires metadata.name")
    hostname = str((frontend.get("spec") or {}).get("hostname") or "").strip()
    return ForetokenDeployment(
        path=path,
        rendered=rendered,
        namespace=next(iter(namespaces)),
        frontend=name,
        hostname=hostname,
        models=models,
        objects=tuple(documents),
    )
