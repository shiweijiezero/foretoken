# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit service intent and retain actual creation ownership across delayed startup writes."""

from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Any

import yaml

from foretoken.applications.files import SOURCE_REVISION
from foretoken.deployment.instances import (
    DEPLOYMENT_RESOURCE,
    DEPLOYMENT_UID_LABEL,
    DeploymentInstance,
    storage_only_instance,
)
from foretoken.deployment.manifest import ForetokenDeployment
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import (
    Kubectl,
    KubernetesVersionConflictError,
    ResourceRef,
    timeout_seconds,
)
from foretoken.kubernetes.mutation import (
    _SERVICE_OPERATION_ANNOTATION,
    DeploymentMutation,
)


def _matches_declared_fields(actual: Any, desired: Any) -> bool:
    """Compare retained storage intent while allowing fields defaulted by its API."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches_declared_fields(actual[key], value)
            for key, value in desired.items()
        )
    return actual == desired


def _intent_version(value: dict[str, Any] | None) -> tuple[str, int, str, str] | None:
    """Identify spec and executable intent independently of status-only API updates."""
    if value is None:
        return None
    metadata = value["metadata"]
    if metadata.get("deletionTimestamp"):
        raise DeploymentError(
            f"DeploymentSuperseded: {value['kind']}/{metadata['name']} is being deleted"
        )
    return (
        metadata["uid"],
        metadata["generation"],
        (metadata.get("annotations") or {}).get(SOURCE_REVISION, ""),
        (metadata.get("annotations") or {}).get(_SERVICE_OPERATION_ANNOTATION, ""),
    )


class DeploymentSubmission:
    """Own submission preconditions and creation receipts for the deployment lifecycle."""

    def __init__(
        self,
        kubectl: Kubectl,
        deployment: ForetokenDeployment,
        timeout: str,
        *,
        create_only: bool = False,
    ) -> None:
        self.kubectl = kubectl
        self.namespace = deployment.namespace
        self.timeout = timeout_seconds(timeout)
        self.operation = uuid.uuid4().hex
        self.instance = DeploymentInstance(kubectl, self.namespace, deployment.name)
        self.instance.uid = deployment.uid
        self.mutation = DeploymentMutation(kubectl, self.namespace, deployment.name, timeout, uid=deployment.uid)
        self.create_only = create_only
        self.deployment = deployment
        self.expected = {
            ref: _intent_version(
                kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
            )
            for ref in deployment.service_refs()
        }
        if create_only and any(
            version is not None for version in self.expected.values()
        ):
            raise DeploymentError(
                "Temporary deployment requires services that do not already exist"
            )
        self.committed: dict[ResourceRef, tuple[str, int, str, str] | None] = {}
        self.created: dict[ResourceRef, dict[str, Any]] = {}
        self._scope: dict[str, Any] | None = None

    def _prepare_instance(self) -> None:
        """Bind all submitted resources to one root before claiming its operation."""
        if self._scope is not None:
            return
        if self.instance.ensure(self.deployment):
            self.created[ResourceRef("InferenceDeployment", self.instance.name, self.namespace)] = self.instance.resource
        self.mutation.uid = self.instance.uid
        self.deployment = self.instance.bind(self.deployment)

    def begin(self) -> None:
        """Claim instance intent without allowing cleanup to interleave with the takeover."""
        self._prepare_instance()
        with self.mutation.hold():
            self._begin()

    def _begin(self) -> None:
        """Advance submitted intent on the instance record under its mutation lock."""
        if self._scope is not None:
            self.check()
            return
        for ref in self.expected:
            self._current(ref)
        ref = ResourceRef("InferenceDeployment", self.instance.name, self.namespace)
        current = self.instance.read()
        if self.create_only and ref not in self.created and not storage_only_instance(current):
            raise DeploymentError(f"Temporary deployment instance {self.instance.name!r} already belongs to another submission")
        deadline = time.monotonic() + self.timeout
        identity = (
            current["metadata"]["uid"],
            current["metadata"].get("annotations", {}).get(_SERVICE_OPERATION_ANNOTATION, ""),
        )
        while True:
            metadata = current["metadata"]
            if metadata.get("deletionTimestamp") or identity != (
                metadata["uid"],
                metadata.get("annotations", {}).get(_SERVICE_OPERATION_ANNOTATION, ""),
            ):
                raise DeploymentError(
                    f"DeploymentSuperseded: instance {self.instance.name!r} was changed by another operation"
                )
            try:
                self._scope = self._claim_operation(ref, current, deadline)
                break
            except KubernetesVersionConflictError:
                latest = self.instance.read(required=False)
                if latest is None or latest["metadata"]["resourceVersion"] == metadata["resourceVersion"]:
                    raise
                current = latest
        self.check()

    def owns_instance(self) -> bool:
        """Tell cleanup whether this operation still owns the same live deployment instance."""
        if self._scope is None:
            return False
        current = self.kubectl.get_if_exists(DEPLOYMENT_RESOURCE, self.instance.name, self.namespace)
        return (
            current is not None
            and not current["metadata"].get("deletionTimestamp")
            and current["metadata"]["uid"] == self._scope["metadata"]["uid"]
            and current["metadata"].get("annotations", {}).get(_SERVICE_OPERATION_ANNOTATION) == self.operation
        )

    def owns_created(self, ref: ResourceRef, current: dict[str, Any] | None) -> bool:
        """Match a recorded creation against current service intent or dependency ownership."""
        if (
            ref not in self.created
            or current is None
            or current["metadata"].get("deletionTimestamp")
            or current["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != self.instance.uid
        ):
            return False
        if ref in self.expected:
            return _intent_version(current) == self.committed.get(ref)
        return (
            current["metadata"]["uid"] == self.created[ref]["metadata"]["uid"]
            and current["metadata"]
            .get("annotations", {})
            .get(_SERVICE_OPERATION_ANNOTATION)
            == self.operation
        )

    def apply(self, rendered: str, *, patch_services: bool = False) -> None:
        """Submit configuration or suspension without interleaving temporary retirement."""
        self._prepare_instance()
        with self.mutation.hold():
            self._begin()
            self._submit(rendered, patch_services=patch_services)

    def _submit(self, rendered: str, *, patch_services: bool) -> None:
        """Record actual creations while applying service and dependency intent under one lock."""
        services = []
        dependencies = []
        for value in yaml.safe_load_all(rendered):
            if value is None:
                continue
            metadata = value.get("metadata") or {}
            ref = ResourceRef(
                value["kind"],
                metadata["name"],
                metadata.get("namespace") or self.namespace,
            )
            if ref in self.expected and str(value.get("apiVersion") or "").startswith(
                "inference.foretoken.io/"
            ):
                services.append((ref, value))
            else:
                dependencies.append(value)
        self.check()
        if patch_services:
            # A stop adopts existing storage without creating it or replaying stale configuration.
            dependencies = [
                value
                for value in self.deployment.objects
                if value["kind"] != "Namespace"
                and ResourceRef(
                    value["kind"],
                    value["metadata"]["name"],
                    value["metadata"].get("namespace") or self.namespace,
                )
                not in self.expected
            ]
        for value in dependencies:
            self._apply_dependency(value, claim_only=patch_services)
        for ref, value in services:
            if ref in self.committed:
                continue
            if patch_services:
                self._apply_service(ref, value, patch=True)
                continue
            suspended = value["spec"].get("suspend", False)
            if suspended and self.expected[ref] is not None:
                self._apply_service(ref, value, patch=True)
            # Keep execution-state writes separate from SSA configuration ownership.
            # Resume only after new configuration is submitted; a newer stop between
            # these stages changes the precondition and prevents the resume.
            self._apply_service(ref, value)
            if not suspended:
                self._apply_service(ref, value, patch=True)

    def check(self) -> None:
        """Confirm instance ownership and every expected service before further writes or observation."""
        if not self.owns_instance():
            raise DeploymentError(
                f"DeploymentSuperseded: instance {self.instance.name!r} was changed by another operation"
            )
        for ref in self.expected:
            self._current(ref)

    def _current(self, ref: ResourceRef) -> dict[str, Any] | None:
        """Read current service intent and reject operations superseded during preparation."""
        value = self._member(ref)
        if _intent_version(value) != self.committed.get(ref, self.expected[ref]):
            raise DeploymentError(
                f"DeploymentSuperseded: {ref.display_name} was changed by another operation"
            )
        return value

    def _member(self, ref: ResourceRef) -> dict[str, Any] | None:
        """Read existing intent without adopting a colliding resource from another instance."""
        current = self.kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
        if current is not None and current["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) != self.instance.uid:
            raise DeploymentError(f"{ref.display_name} belongs to another deployment instance")
        return current

    def _claim_operation(
        self,
        ref: ResourceRef,
        current: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        """CAS operation metadata independently of SSA-owned configuration fields."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeploymentError(f"Timed out claiming {ref.display_name}")
        metadata = current["metadata"]
        payload = {"metadata": {
            "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
            "annotations": {_SERVICE_OPERATION_ANNOTATION: self.operation},
        }}
        if self.create_only and ref.kind == "InferenceDeployment":
            # Claim retained storage and serving intent in one CAS. A second
            # temporary caller then sees declared services, even before creation.
            payload["spec"] = {"resources": self.deployment.resource_references()}
        result = self.kubectl.run(
            [
                "patch",
                ref.kind,
                ref.name,
                *(["--namespace", ref.namespace] if ref.namespace else []),
                "--type=merge",
                "--field-manager=kubectl",
                "--output=json",
                "--patch",
                json.dumps(payload),
            ],
            timeout=remaining,
        )
        return json.loads(result.stdout)

    def _apply_dependency(self, value: dict[str, Any], *, claim_only: bool) -> None:
        """Apply dependencies with creation receipts; stops claim only existing metadata."""
        metadata = value["metadata"]
        ref = ResourceRef(
            value["kind"],
            metadata["name"],
            metadata.get("namespace") or self.namespace,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            self.check()
            current = self._member(ref)
            created_ref = (
                ref
                if current is None
                else ResourceRef(
                    current["kind"],
                    current["metadata"]["name"],
                    current["metadata"].get("namespace", ""),
                )
            )
            receipt = self.created.get(created_ref)
            if claim_only and current is None:
                return
            if self.create_only and current is not None and (
                receipt is None or receipt["metadata"]["uid"] != current["metadata"]["uid"]
            ):
                if not _matches_declared_fields(current.get("spec", {}), value.get("spec", {})):
                    raise DeploymentError(
                        f"Retained {ref.display_name} differs from the submitted storage configuration; "
                        "deploy the instance to apply storage changes before benchmarking"
                    )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError(f"Timed out submitting {ref.display_name}")
            payload = copy.deepcopy(value)
            payload["metadata"].setdefault("annotations", {})[
                _SERVICE_OPERATION_ANNOTATION
            ] = self.operation
            created = current is None
            try:
                if created:
                    # Native resources permit unconditional SSA updates with version
                    # zero. Create provides the atomic absence check and receipt.
                    payload["metadata"].pop("resourceVersion", None)
                    current = json.loads(
                        self.kubectl.run(
                            [
                                "create",
                                "--save-config",
                                "--field-manager=kubectl",
                                "--namespace",
                                self.namespace,
                                "--filename=-",
                                "--output=json",
                            ],
                            input_text=yaml.safe_dump(payload, sort_keys=False),
                            timeout=remaining,
                        ).stdout
                    )
                    identity = current["metadata"]
                    created_ref = ResourceRef(
                        current["kind"], identity["name"], identity.get("namespace", "")
                    )
                    self.created[created_ref] = current
                current = self._claim_operation(ref, current, deadline)
                if (
                    created_ref in self.created
                    and self.created[created_ref]["metadata"]["uid"]
                    == current["metadata"]["uid"]
                ):
                    self.created[created_ref] = current
                if not claim_only and not created:
                    payload["metadata"].update(
                        uid=current["metadata"]["uid"],
                        resourceVersion=current["metadata"]["resourceVersion"],
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeploymentError(
                            f"Timed out submitting {ref.display_name}"
                        )
                    self.kubectl.run(
                        [
                            "apply",
                            "--server-side",
                            "--namespace",
                            self.namespace,
                            "--filename=-",
                            "--output=json",
                        ],
                        input_text=yaml.safe_dump(payload, sort_keys=False),
                        timeout=remaining,
                    )
                return
            except KubernetesVersionConflictError:
                latest = self.kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
                if (
                    current is None
                    or latest is None
                    or latest["metadata"]["resourceVersion"]
                    == current["metadata"]["resourceVersion"]
                ):
                    raise

    def _apply_service(
        self, ref: ResourceRef, value: dict[str, Any], *, patch: bool = False
    ) -> None:
        """Submit one stage and advance its precondition, retrying only status-update races."""
        deadline = time.monotonic() + self.timeout
        suspended = value["spec"].get("suspend", False)
        while True:
            self.check()
            current = self._current(ref)
            payload = copy.deepcopy(value)
            if patch and current is not None:
                payload = {
                    "apiVersion": current["apiVersion"],
                    "kind": ref.kind,
                    "metadata": {"name": ref.name, "namespace": ref.namespace},
                    "spec": {"suspend": suspended},
                }
                # Repeating stop supersedes older preparation without selecting a different application.
                payload["metadata"].setdefault("annotations", {})[
                    _SERVICE_OPERATION_ANNOTATION
                ] = self.operation
            else:
                payload["metadata"].get("annotations", {}).pop(
                    _SERVICE_OPERATION_ANNOTATION, None
                )
                if current is not None and not suspended:
                    payload["spec"].pop("suspend", None)
            if current is None:
                # SSA version zero permits creation but cannot update an existing CR.
                payload["metadata"]["resourceVersion"] = "0"
            else:
                payload["metadata"].update(
                    uid=current["metadata"]["uid"],
                    resourceVersion=current["metadata"]["resourceVersion"],
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError(f"Timed out submitting {ref.display_name}")
            try:
                if patch and current is not None:
                    applied = self.kubectl.run(
                        [
                            "patch",
                            ref.kind,
                            ref.name,
                            "--namespace",
                            ref.namespace,
                            "--type=merge",
                            "--field-manager=kubectl",
                            "--output=json",
                            "--patch",
                            json.dumps(payload),
                        ],
                        timeout=remaining,
                    )
                else:
                    applied = self.kubectl.run(
                        ["apply", "--server-side", "--filename=-", "--output=json"],
                        input_text=yaml.safe_dump(payload, sort_keys=False),
                        timeout=remaining,
                    )
                result = json.loads(applied.stdout)
                if current is None or ref in self.created:
                    self.created[ref] = result
                self.committed[ref] = _intent_version(result)
                if patch and current is None:
                    # Creation establishes configuration; the metadata operation belongs to a patch.
                    continue
                if patch and result.get("spec", {}).get("suspend") is not suspended:
                    raise DeploymentError(
                        "The platform does not support service suspension; update the platform first"
                    )
                return
            except KubernetesVersionConflictError:
                latest = self._current(ref)
                if (
                    current is None
                    or latest["metadata"]["resourceVersion"]
                    == current["metadata"]["resourceVersion"]
                ):
                    # Field ownership conflicts are not optimistic-version races.
                    raise
