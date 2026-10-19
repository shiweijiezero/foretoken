# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Address deployment instances without taking over controller or service lifecycles."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from foretoken.deployment.manifest import (
    DEPLOYMENT_API_VERSION,
    DEPLOYMENT_KIND,
    DEPLOYMENT_UID_LABEL,
    ForetokenDeployment,
    RuntimeCacheManifest,
    load_deployment,
    project_deployment,
)
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import (
    Kubectl,
    KubernetesVersionConflictError,
    timeout_seconds,
)

DEPLOYMENT_RESOURCE = "inferencedeployments.inference.foretoken.io"


def storage_only_instance(resource: dict[str, Any]) -> bool:
    """Identify retained storage with no declared service intent before a temporary submission."""
    references = resource.get("spec", {}).get("resources", [])
    return bool(resource.get("spec", {}).get("updateID")) and all(
        (reference["apiVersion"], reference["kind"]) in {
            (DEPLOYMENT_API_VERSION, "RuntimeCache"), ("v1", "PersistentVolumeClaim"),
        }
        for reference in references
    )


class DeploymentInstance:
    """Read and request one namespaced instance; callers own submission and retirement order."""

    def __init__(self, kubectl: Kubectl, namespace: str, name: str) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.name = name
        self.uid = ""
        self.resource: dict[str, Any] | None = None

    @classmethod
    def list(cls, kubectl: Kubectl, *, all_namespaces: bool = False) -> tuple[DeploymentInstance, ...]:
        """List current-namespace instances, or an administrator's explicit platform-wide inventory."""
        resources = (
            kubectl.list_all_resources((DEPLOYMENT_RESOURCE,))
            if all_namespaces else kubectl.list_resources((DEPLOYMENT_RESOURCE,), kubectl.current_namespace())
        )
        instances = []
        for resource in resources:
            metadata = resource["metadata"]
            instance = cls(kubectl, metadata["namespace"], metadata["name"])
            instance.uid, instance.resource = metadata["uid"], resource
            instances.append(instance)
        return tuple(instances)

    @property
    def controller_name(self) -> str:
        """Return the platform-owned controller and access ConfigMap name after reading the instance."""
        assert self.uid, "deployment instance must be read before selecting its controller"
        return "foretoken-" + self.uid

    def read(self, *, required: bool = True) -> dict[str, Any] | None:
        """Read the selected App, retaining its UID so a replacement cannot inherit this operation."""
        current = self.kubectl.get_if_exists(DEPLOYMENT_RESOURCE, self.name, self.namespace)
        if current is None:
            if required:
                raise DeploymentError(f"deployment instance {self.name!r} was not found")
            return None
        metadata = current["metadata"]
        if self.uid and metadata["uid"] != self.uid:
            raise DeploymentError(f"DeploymentSuperseded: deployment instance {self.name!r} was replaced")
        self.uid = metadata["uid"]
        self.resource = current
        return current

    def ensure(self, deployment: ForetokenDeployment) -> bool:
        """Create the instance membership record once, before callers publish or submit applications."""
        current = self.read(required=False)
        if current is not None:
            if current["metadata"].get("deletionTimestamp"):
                raise DeploymentError(f"deployment instance {self.name!r} is being deleted")
            return False
        if self.uid:
            raise DeploymentError(f"DeploymentSuperseded: deployment instance {self.name!r} was deleted")
        document = {
            "apiVersion": DEPLOYMENT_API_VERSION,
            "kind": DEPLOYMENT_KIND,
            "metadata": {"name": self.name, "namespace": self.namespace},
            "spec": {"resources": deployment.resource_references()},
        }
        try:
            self.resource = json.loads(self.kubectl.run(
                ["create", "--filename=-", "--output=json"],
                input_text=json.dumps(document),
            ).stdout)
        except DeploymentError as error:
            if "(AlreadyExists)" not in str(error):
                raise
            self.read()
            return False
        self.uid = self.resource["metadata"]["uid"]
        return True

    def request(
        self, deployment: ForetokenDeployment, timeout: str, *, update_id: str,
        controller_source_revision: str = "",
    ) -> None:
        """Select published controller code and root membership using the caller's update identity."""
        deadline = time.monotonic() + timeout_seconds(timeout)
        with self.kubectl.until(deadline):
            while time.monotonic() < deadline:
                current = self.read()
                if current["metadata"].get("deletionTimestamp"):
                    raise DeploymentError(f"deployment instance {self.name!r} is being deleted")
                payload = {
                    "metadata": {"uid": self.uid, "resourceVersion": current["metadata"]["resourceVersion"]},
                    "spec": {
                        "updateID": update_id,
                        "controllerSourceRevision": controller_source_revision,
                        "resources": deployment.resource_references(),
                    },
                }
                try:
                    self.resource = json.loads(self.kubectl.run(
                        ["patch", DEPLOYMENT_RESOURCE, self.name, "--namespace", self.namespace,
                         "--type=merge", "--patch", json.dumps(payload), "--output=json"],
                    ).stdout)
                    return
                except KubernetesVersionConflictError:
                    continue
        raise DeploymentError(f"timed out updating deployment instance {self.name!r}")

    def wait_ready(self, timeout: str, *, observe: Callable[[], None] | None = None) -> str:
        """Wait for the requested controller revision while allowing the caller to reject superseded intent."""
        assert self.resource is not None, "deployment request must be read or submitted before waiting"
        expected = (
            self.resource["metadata"]["uid"], self.resource["metadata"]["generation"],
            self.resource.get("spec", {}).get("updateID", ""),
        )
        deadline = time.monotonic() + timeout_seconds(timeout)
        message = "Waiting for the deployment controller"
        with self.kubectl.until(deadline):
            while time.monotonic() < deadline:
                current = self.read()
                if observe is not None:
                    observe()
                observed = (
                    current["metadata"]["uid"], current["metadata"]["generation"],
                    current.get("spec", {}).get("updateID", ""),
                )
                if observed != expected:
                    raise DeploymentError(f"DeploymentSuperseded: deployment instance {self.name!r} has a newer request")
                if current["metadata"].get("deletionTimestamp"):
                    raise DeploymentError(f"deployment instance {self.name!r} is being deleted")
                status = current.get("status") or {}
                ready = next(
                    (condition for condition in status.get("conditions", []) if condition["type"] == "Ready"),
                    None,
                )
                if ready is not None:
                    message = ready.get("message") or ready.get("reason") or message
                if (
                    status.get("observedGeneration") == expected[1]
                    and ready is not None
                    and ready.get("observedGeneration") == expected[1]
                ):
                    if ready["status"] == "False":
                        raise DeploymentError(f"deployment instance {self.name!r} rejected its configuration: {message}")
                    if ready["status"] == "True" and status.get("controllerRevision"):
                        return status["controllerRevision"]
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise DeploymentError(f"timed out waiting for deployment instance {self.name!r}: {message}")

    def delete(self, timeout: str, *, wait: bool = True) -> None:
        """Request deletion of this exact App UID; the platform owns service drain and controller retirement."""
        deadline = time.monotonic() + timeout_seconds(timeout)
        with self.kubectl.until(deadline):
            while time.monotonic() < deadline:
                current = self.read(required=False)
                if current is None:
                    return
                if current["metadata"].get("deletionTimestamp"):
                    break
                try:
                    self.kubectl.delete_object(
                        current, f"{max(1, int(deadline - time.monotonic()))}s", wait=False, propagation="Background",
                    )
                    break
                except KubernetesVersionConflictError:
                    continue
                except DeploymentError:
                    remaining = self.kubectl.get_if_exists(DEPLOYMENT_RESOURCE, self.name, self.namespace)
                    if remaining is not None and remaining["metadata"]["uid"] == self.uid:
                        raise
                    return
            else:
                raise DeploymentError(f"timed out deleting deployment instance {self.name!r}")
        if wait:
            self.wait_deleted(f"{max(1, int(deadline - time.monotonic()))}s")

    def wait_deleted(self, timeout: str) -> None:
        """Wait for the selected App UID to disappear without waiting on a reused instance name."""
        if not self.uid and self.read(required=False) is None:
            return
        self.kubectl.wait_deleted(
            DEPLOYMENT_RESOURCE,
            {"name": self.name, "namespace": self.namespace, "uid": self.uid}, timeout,
        )

    def access(self) -> dict[str, Any]:
        """Read controller-published storage and Gateway access for this exact instance."""
        self.read()
        configuration = self.kubectl.get_if_exists("configmap", self.controller_name, self.namespace)
        payload = (configuration or {}).get("data", {}).get("deployment-access.json")
        if not payload:
            raise DeploymentError(f"deployment access for {self.name!r} is not published")
        return json.loads(payload)

    def bind(self, deployment: ForetokenDeployment) -> ForetokenDeployment:
        """Attach the App UID to root resources and native workload selectors before submission."""
        if (deployment.namespace, deployment.name) != (self.namespace, self.name):
            raise DeploymentError("deployment membership does not match the selected instance")
        self.read()
        if deployment.uid and deployment.uid != self.uid:
            raise DeploymentError("deployment instance identity changed before submission")
        bound = deployment if deployment.uid else project_deployment(deployment, self.kubectl, uid=self.uid)
        assert self.resource is not None
        owner = {
            "apiVersion": self.resource["apiVersion"], "kind": self.resource["kind"],
            "name": self.resource["metadata"]["name"], "uid": self.resource["metadata"]["uid"],
            "controller": True, "blockOwnerDeletion": True,
        }
        objects = copy.deepcopy(bound.objects)
        for document in objects:
            metadata = document["metadata"]
            references = metadata.get("ownerReferences") or []
            for reference in references:
                if reference.get("controller") and any(reference.get(key) != owner[key] for key in ("apiVersion", "kind", "name", "uid")):
                    raise DeploymentError(f"{document['kind']}/{metadata['name']} already has another controller owner")
            metadata["ownerReferences"] = [reference for reference in references if reference.get("uid") != self.uid] + [dict(owner)]
        # Membership changes only metadata, including reference-only objects from an interrupted retirement.
        return replace(bound, objects=objects, rendered=yaml.safe_dump_all(objects, sort_keys=False))

    def deployment(self, *, allow_missing: bool = False, missing_instance_ok: bool = False) -> ForetokenDeployment:
        """Recover actual intent, or an unbound identity for retrying deletion of an absent instance."""
        current = self.read(required=not missing_instance_ok)
        if current is None:
            return ForetokenDeployment(
                path=None, rendered="", namespace=self.namespace, frontend="", hostname="",
                models={}, runtime_caches=(), objects=(), name=self.name,
            )
        objects = []
        models = {}
        caches = []
        frontend = ""
        hostname = ""
        for reference in current.get("spec", {}).get("resources", []):
            kind, version, name = reference["kind"], reference["apiVersion"], reference["name"]
            if kind in {"Namespace", DEPLOYMENT_KIND}:
                raise DeploymentError(f"invalid deployment instance member {kind}/{name}")
            document = None
            if version.startswith("inference.foretoken.io/") and kind in {"FrontendService", "ModelService", "RuntimeCache"}:
                document = self.kubectl.get_if_exists(kind + ".inference.foretoken.io", name, self.namespace)
                if document is None and not allow_missing:
                    raise DeploymentError(f"deployment instance {self.name!r} has no {kind}/{name} yet")
                if document is not None and document["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != self.uid:
                    raise DeploymentError(f"{kind}/{name} does not belong to deployment instance {self.name!r}")
            if kind == "FrontendService" and version.startswith("inference.foretoken.io/"):
                if frontend:
                    raise DeploymentError(f"deployment instance {self.name!r} declares multiple frontends")
                frontend = name
                if document is not None:
                    hostname = document["spec"].get("hostname", "")
            elif kind == "ModelService" and document is not None:
                models[name] = document["spec"]["model"]
            elif kind == "RuntimeCache" and document is not None:
                spec = document["spec"]
                caches.append(RuntimeCacheManifest(
                    name, self.namespace, spec.get("directory", ""),
                    spec.get("accessMode", "ReadWriteMany"), spec.get("retentionPolicy", "Retain"),
                ))
            if document is None:
                document = {
                    "apiVersion": version, "kind": kind,
                    "metadata": {"name": name, "namespace": self.namespace},
                }
            objects.append(document)
        if not frontend and not allow_missing:
            raise DeploymentError(f"deployment instance {self.name!r} has no declared frontend")
        return ForetokenDeployment(
            path=None, rendered=yaml.safe_dump_all(objects, sort_keys=False), namespace=self.namespace,
            frontend=frontend, hostname=hostname, models=models, runtime_caches=tuple(caches),
            objects=tuple(objects), name=self.name, uid=self.uid,
        )


def resolve_deployment_target(
    target: str, kubectl: Kubectl, *, name: str | None = None, allow_missing: bool = False,
    missing_instance_ok: bool = False,
) -> ForetokenDeployment:
    """Resolve a Kustomize path or a readable instance name only within the current kubeconfig namespace."""
    path = Path(target).expanduser()
    if path.exists() or "/" in target or target.startswith((".", "~")):
        desired = load_deployment(target, kubectl, name=name)
        current = DeploymentInstance(kubectl, desired.namespace, desired.name).deployment(allow_missing=allow_missing, missing_instance_ok=missing_instance_ok)
        return replace(current, path=desired.path)
    if name is not None:
        raise DeploymentError("--name selects an instance for a Kustomize path; otherwise pass the instance name directly")
    return DeploymentInstance(kubectl, kubectl.current_namespace(), target).deployment(allow_missing=allow_missing, missing_instance_ok=missing_instance_ok)
