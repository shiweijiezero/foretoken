# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read administrator-owned namespace settings for service operations."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError, ForetokenDeployment, parse_deployment
from foretoken.platform.config import default_platform_config
from foretoken.storage import REMOTE_RUNTIME_CACHE_SIZE


def namespace_controller_name() -> str:
    """Return the CLI release's controller and configuration name in a serving namespace."""
    return default_platform_config().platform.release_name + "-control-plane"


@dataclass(frozen=True)
class NamespaceAccess:
    """Describe administrator-owned resources that service commands consume but never retire."""

    namespace: str
    cache_claim: str
    cache_access_mode: str
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
            namespace, values["cacheClaim"], values["cacheAccessMode"], values["gateway"],
        )

    def select_resources(self, deployment: ForetokenDeployment, *, deleting: bool = False) -> ForetokenDeployment:
        """Keep service intent separate from administrator resources while allowing old managed caches to retire."""
        objects = []
        for original in deployment.objects:
            if original["kind"] == "Namespace":
                continue
            document = copy.deepcopy(original)
            if document["kind"] == "RuntimeCache" and document["apiVersion"].startswith("inference.foretoken.io/"):
                if self.cache_claim and not deleting:
                    continue
                spec = document["spec"]
                directory = spec.get("directory")
                if deleting:
                    # Retire the declared CR, not a static PV managed by the administrator.
                    spec.pop("directory", None)
                elif directory:
                    if Path(directory).is_absolute():
                        raise DeploymentError(
                            f"Namespace {self.namespace} requires an administrator-provided PVC for host directories"
                        )
                    spec.pop("directory")
                    document["metadata"].get("annotations", {}).pop("inference.foretoken.io/directory-owner", None)
                    spec.setdefault("initialSize", REMOTE_RUNTIME_CACHE_SIZE)
                    if self.cache_access_mode == "ReadWriteOnce" and spec.get("accessMode", "ReadWriteMany") == "ReadWriteMany":
                        spec["accessMode"] = "ReadWriteOnce"
            objects.append(document)
        return parse_deployment(deployment.path, yaml.safe_dump_all(objects, sort_keys=False))
