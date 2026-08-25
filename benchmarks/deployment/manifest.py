# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Kustomize parsing for Foretoken benchmark service discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class DeploymentError(RuntimeError):
    """A deployment configuration cannot identify a reachable service."""


@dataclass(frozen=True)
class DeploymentResources:
    """Public serving resources rendered from one deployment configuration."""

    namespace: str
    frontend: str
    hostname: str
    models: dict[str, str]
    objects: tuple[dict[str, Any], ...]


def deployment_path(path_value: str) -> Path:
    """Return a validated Kustomize root path."""
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise DeploymentError(f"deployment directory not found: {path}")
    names = ("kustomization.yaml", "kustomization.yml", "Kustomization")
    if not any((path / name).is_file() for name in names):
        raise DeploymentError(f"deployment directory is not a Kustomize root: {path}")
    return path


def parse_deployment(path: Path, rendered: str) -> DeploymentResources:
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
        kind = document.get("kind")
        if kind in {"FrontendService", "ModelService"}:
            namespaces.add(str(metadata.get("namespace") or "").strip())
        if kind == "FrontendService":
            frontends.append(document)
        elif kind == "ModelService":
            name = str(metadata.get("name") or "").strip()
            model = str((document.get("spec") or {}).get("model") or "").strip()
            if not name or not model:
                raise DeploymentError(
                    "each ModelService requires metadata.name and spec.model"
                )
            models[name] = model

    if len(frontends) != 1:
        raise DeploymentError("a deployment must render exactly one FrontendService")
    if not models:
        raise DeploymentError("a deployment must render at least one ModelService")
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
    return DeploymentResources(
        namespace=next(iter(namespaces)),
        frontend=name,
        hostname=hostname,
        models=models,
        objects=tuple(documents),
    )
