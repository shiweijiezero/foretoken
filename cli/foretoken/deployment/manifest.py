# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render and identify Foretoken serving resources from Kustomize."""

from __future__ import annotations

import copy
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef

DEPLOYMENT_API_VERSION = "inference.foretoken.io/v1alpha1"
DEPLOYMENT_KIND = "InferenceDeployment"
DEPLOYMENT_UID_LABEL = "inference.foretoken.io/deployment-uid"


@dataclass(frozen=True)
class RuntimeCacheManifest:
    """One RuntimeCache manifest that may request a local directory volume."""

    name: str
    namespace: str
    directory: str
    access_mode: str
    retention_policy: str


@dataclass(frozen=True)
class ForetokenDeployment:
    """Rendered Kustomize payload and its user-facing Foretoken services."""

    path: Path | None
    rendered: str
    namespace: str
    frontend: str
    hostname: str
    models: dict[str, str]
    runtime_caches: tuple[RuntimeCacheManifest, ...]
    objects: tuple[dict[str, Any], ...]
    name: str
    uid: str = ""

    def service_refs(self) -> tuple[ResourceRef, ...]:
        """Return declared services, including references whose objects have already retired."""
        return tuple(
            ResourceRef(kind, document["metadata"]["name"], self.namespace)
            for kind in ("FrontendService", "ModelService")
            for document in self.objects
            if document.get("kind") == kind
            and document.get("apiVersion", "").startswith("inference.foretoken.io/")
        )

    def with_objects(self, objects: Iterable[dict[str, Any]]) -> ForetokenDeployment:
        """Reindex changed typed intent without changing its source path or instance identity."""
        return parse_deployment(
            self.path, yaml.safe_dump_all(objects, sort_keys=False), name=self.name, uid=self.uid,
        )

    def resource_references(self) -> list[dict[str, str]]:
        """Record root membership for InferenceDeployment without copying typed configuration."""
        return [
            {
                "apiVersion": document["apiVersion"],
                "kind": document["kind"],
                "name": document["metadata"]["name"],
            }
            for document in self.objects
            if document["kind"] not in {"Namespace", DEPLOYMENT_KIND}
        ]


def service_requires_execution(service: dict[str, Any]) -> bool:
    """Tell deployment preparation whether a model or frontend needs running instances."""
    spec = service.get("spec") or {}
    if spec.get("suspend", False):
        return False
    if service["kind"] == "ModelService":
        if spec.get("autoscaling") is not None:
            return True
        pools = spec.get("modelPools")
        if pools:
            return any(pool.get("replicas", 1) != 0 for pool in pools)
    return spec.get("replicas", 1) != 0


def deployment_path(path_value: str) -> Path:
    """Return a validated Kustomize root supplied by a CLI caller."""
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise DeploymentError(f"deployment directory not found: {path}")
    names = ("kustomization.yaml", "kustomization.yml", "Kustomization")
    if not any((path / name).is_file() for name in names):
        raise DeploymentError(f"deployment directory is not a Kustomize root: {path}")
    return path


def parse_deployment(
    path: Path | None, rendered: str, *, name: str | None = None, uid: str = "",
) -> ForetokenDeployment:
    """Index rendered service intent while retaining its deployment instance identity."""
    instance_name = name
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise DeploymentError(f"deployment rendered invalid YAML: {exc}") from exc
    if not documents:
        raise DeploymentError(f"deployment rendered no Kubernetes resources: {path}")

    frontends: list[dict[str, Any]] = []
    models: dict[str, str] = {}
    runtime_caches: list[RuntimeCacheManifest] = []
    namespaces: set[str] = set()
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise DeploymentError(
                f"rendered document {index} is not a Kubernetes object"
            )
        metadata = document.get("metadata") or {}
        api_version = str(document.get("apiVersion") or "")
        kind = document.get("kind")
        if not api_version.startswith("inference.foretoken.io/"):
            continue

        namespace = str(metadata.get("namespace") or "").strip()
        if kind == "RuntimeCache":
            name = str(metadata.get("name") or "").strip()
            spec = document.get("spec") or {}
            if not name or not namespace or not isinstance(spec, dict):
                raise DeploymentError(
                    "RuntimeCache requires metadata.name, metadata.namespace, and spec"
                )
            directory = spec.get("directory", "")
            if not isinstance(directory, str) or (
                "directory" in spec and not directory
            ):
                raise DeploymentError(
                    f"RuntimeCache/{name} directory must be a nonempty path string"
                )
            runtime_caches.append(
                RuntimeCacheManifest(
                    name=name,
                    namespace=namespace,
                    directory=directory,
                    access_mode=str(spec.get("accessMode") or "ReadWriteMany"),
                    retention_policy=str(spec.get("retentionPolicy") or "Retain"),
                )
            )
            continue
        if kind not in {"FrontendService", "ModelService"}:
            continue

        namespaces.add(namespace)
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
    namespace = next(iter(namespaces))
    if any(cache.namespace != namespace for cache in runtime_caches):
        raise DeploymentError(
            "RuntimeCache resources must share the FrontendService and ModelService namespace"
        )
    if len(runtime_caches) > 1:
        raise DeploymentError("a deployment may render at most one RuntimeCache")
    hostname = str((frontend.get("spec") or {}).get("hostname") or "").strip()
    return ForetokenDeployment(
        path=path,
        rendered=rendered,
        namespace=namespace,
        frontend=name,
        hostname=hostname,
        models=models,
        runtime_caches=tuple(runtime_caches),
        objects=tuple(documents),
        name=instance_name or name,
        uid=uid,
    )


def _rewrite_foretoken_references(objects: list[dict[str, Any]], prefix: str) -> None:
    """Project Foretoken's declared local references; Kubernetes owns native reference rewriting."""
    names = {
        (
            document["apiVersion"].split("/")[0] if "/" in document["apiVersion"] else "",
            document["kind"],
            document["metadata"]["name"],
        ): prefix + document["metadata"]["name"]
        for document in objects
    }

    def reference(container: Any, field: str, group: str, kind: str) -> None:
        if isinstance(container, dict) and isinstance(container.get(field), str):
            selected = names.get((group, kind, container[field]))
            if selected is not None:
                container[field] = selected

    for document in objects:
        if not document["apiVersion"].startswith("inference.foretoken.io/"):
            continue
        spec = document.get("spec")
        if not isinstance(spec, dict):
            continue
        kind = document["kind"]
        if kind == "ModelService":
            pools = spec.get("modelPools")
            for pool in (spec, *(pools if isinstance(pools, list) else ())):
                if not isinstance(pool, dict):
                    continue
                cache = pool.get("kvCache")
                store = cache.get("mooncakeStore") if isinstance(cache, dict) else None
                if isinstance(store, dict):
                    reference(store.get("kvServiceRef"), "name", "inference.foretoken.io", "KVService")
                environment = pool.get("env")
                if not isinstance(environment, dict):
                    continue
                for value in environment.values():
                    if not isinstance(value, dict):
                        continue
                    source = value.get("valueFrom")
                    if isinstance(source, dict):
                        reference(source.get("secretKeyRef"), "name", "", "Secret")
                        reference(source.get("configMapKeyRef"), "name", "", "ConfigMap")
        elif kind == "FrontendService":
            reference(spec.get("videoTasks"), "claimName", "", "PersistentVolumeClaim")
        elif kind in {"ProfileRun", "VideoTask"}:
            reference(spec.get("modelServiceRef"), "name", "inference.foretoken.io", "ModelService")
            if kind == "VideoTask":
                reference(spec.get("worker"), "outputClaimName", "", "PersistentVolumeClaim")


def project_deployment(
    deployment: ForetokenDeployment, kubectl: Kubectl, *, prefix: str = "", uid: str = "",
) -> ForetokenDeployment:
    """Apply instance naming or UID membership through Kubernetes' native Kustomize transformers."""
    objects = copy.deepcopy(list(deployment.objects))
    if prefix:
        _rewrite_foretoken_references(objects, prefix)
    configuration: dict[str, Any] = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "namespace": deployment.namespace, "resources": ["resources.yaml"],
    }
    if prefix:
        configuration["namePrefix"] = prefix
    if uid:
        configuration["labels"] = [{
            "pairs": {DEPLOYMENT_UID_LABEL: uid},
            "includeSelectors": True, "includeTemplates": True,
        }]
    with tempfile.TemporaryDirectory(prefix="foretoken-instance-") as folder:
        root = Path(folder)
        (root / "resources.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
        (root / "kustomization.yaml").write_text(yaml.safe_dump(configuration, sort_keys=False))
        rendered = kubectl.kustomize(root)
    if prefix:
        # The singleton frontend is the readable instance anchor; other roots retain their prefixed names.
        projected_objects = list(yaml.safe_load_all(rendered))
        for document in projected_objects:
            if document["kind"] == "FrontendService" and document["apiVersion"].startswith("inference.foretoken.io/"):
                document["metadata"]["name"] = deployment.name
        rendered = yaml.safe_dump_all(projected_objects, sort_keys=False)
    projected = parse_deployment(deployment.path, rendered, name=deployment.name, uid=uid or deployment.uid)
    if any(document["metadata"].get("namespace") != deployment.namespace for document in projected.objects):
        raise DeploymentError("deployment resources must belong to the current Kubernetes namespace")
    return projected


def validate_instance_name(name: str) -> str:
    """Apply Kubernetes' default DNS-1035 Service-name constraint to the frontend address anchor."""
    if len(name) > 63 or re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", name) is None:
        raise DeploymentError(
            "instance name must be a Kubernetes Service name: at most 63 lowercase letters, digits or '-', "
            "starting with a letter and ending with a letter or digit"
        )
    return name


def load_deployment(path_value: str, kubectl: Kubectl, *, name: str | None = None) -> ForetokenDeployment:
    """Render instance-scoped intent in the current namespace without owning Namespace resources."""
    path = deployment_path(path_value)
    namespace = kubectl.current_namespace()
    rendered = kubectl.kustomize(path)
    try:
        objects = [document for document in yaml.safe_load_all(rendered) if document is not None]
    except yaml.YAMLError as error:
        raise DeploymentError(f"deployment rendered invalid YAML: {error}") from error
    retained = []
    for document in objects:
        if not isinstance(document, dict):
            raise DeploymentError("deployment rendered a value that is not a Kubernetes object")
        if document.get("kind") == "Namespace":
            continue
        if document.get("kind") == DEPLOYMENT_KIND and document.get("apiVersion", "").startswith("inference.foretoken.io/"):
            raise DeploymentError("InferenceDeployment is managed by foretoken deploy; declare its service resources instead")
        if document.get("apiVersion", "").startswith("inference.foretoken.io/"):
            document.setdefault("metadata", {})["namespace"] = namespace
        retained.append(document)
    deployment = parse_deployment(path, yaml.safe_dump_all(retained, sort_keys=False), name=name)
    validate_instance_name(deployment.name)
    return project_deployment(deployment, kubectl, prefix=deployment.name + "-")
