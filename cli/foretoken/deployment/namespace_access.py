# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read administrator-owned namespace settings for service operations."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import yaml

from foretoken.deployment.manifest import ForetokenDeployment, parse_deployment
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.platform.config import default_platform_config
from foretoken.working_directory import resolve_runtime_directory


def namespace_controller_name() -> str:
    """Return the CLI release's controller and configuration name in a serving namespace."""
    return default_platform_config().platform.release_name + "-control-plane"


@dataclass(frozen=True)
class NamespaceAccess:
    """Describe administrator-owned resources that service commands consume but never retire."""

    namespace: str
    cache_claim: str
    directory_bindings: dict[str, str]
    gateway: dict[str, Any]

    @classmethod
    def read(cls, kubectl: Kubectl, namespace: str) -> NamespaceAccess | None:
        """Read the namespace's explicit service-access mode without consulting platform resources."""
        controller = namespace_controller_name()
        configuration = kubectl.get_if_exists("configmap", controller, namespace)
        payload = (configuration or {}).get("data", {}).get("namespace-access.json")
        if payload is None:
            return None
        values = json.loads(payload)
        return cls(
            namespace, values["cacheClaim"], values["directoryBindings"], values["gateway"],
        )

    def select_resources(self, deployment: ForetokenDeployment, *, deleting: bool = False) -> ForetokenDeployment:
        """Resolve member directory intent against prepared claims without changing its storage selection."""
        objects = []
        for original in deployment.objects:
            if original["kind"] == "Namespace":
                continue
            document = copy.deepcopy(original)
            if document["kind"] == "RuntimeCache" and document["apiVersion"].startswith("inference.foretoken.io/"):
                spec = document["spec"]
                directory = spec.get("directory")
                if self.cache_claim and not directory and not deleting:
                    continue
                document["metadata"].get("annotations", {}).pop("inference.foretoken.io/directory-owner", None)
                if deleting:
                    # Namespace preparation, not a member deployment, owns directory PVs.
                    spec.pop("directory", None)
                elif directory:
                    resolved = str(resolve_runtime_directory(directory, deployment.path))
                    if resolved not in self.directory_bindings:
                        raise DeploymentError(
                            f"Namespace {self.namespace} has no prepared claim for directory {resolved}; "
                            "ask its administrator to authorize that directory"
                        )
                    spec["directory"] = resolved
            objects.append(document)
        return parse_deployment(deployment.path, yaml.safe_dump_all(objects, sort_keys=False))
