# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Submit service intent without replacing a newer operation during source preparation."""

from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Any

import yaml

from foretoken.application_files import SOURCE_REVISION
from foretoken.kubernetes import (
    Kubectl,
    KubernetesVersionConflictError,
    timeout_seconds,
)
from foretoken.manifest import DeploymentError, ForetokenDeployment, ResourceRef

_SERVICE_OPERATION_ANNOTATION = "inference.foretoken.io/service-operation"


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
    """Own one deployment's service preconditions across retirement and delayed startup writes."""

    def __init__(self, kubectl: Kubectl, deployment: ForetokenDeployment, timeout: str) -> None:
        self.kubectl = kubectl
        self.namespace = deployment.namespace
        self.timeout = timeout_seconds(timeout)
        self.operation = uuid.uuid4().hex
        self.expected = {
            ref: _intent_version(kubectl.get_if_exists(ref.kind, ref.name, ref.namespace))
            for ref in deployment.service_refs()
        }
        self.committed: dict[ResourceRef, tuple[str, int, str, str] | None] = {}

    def apply(self, rendered: str, *, patch_services: bool = False) -> None:
        """Submit services once, optionally patching only lifecycle intent, without replacing newer work."""
        services = []
        dependencies = []
        for value in yaml.safe_load_all(rendered):
            metadata = value.get("metadata") or {}
            ref = ResourceRef(value["kind"], metadata["name"], metadata.get("namespace") or self.namespace)
            if ref in self.expected and str(value.get("apiVersion") or "").startswith("inference.foretoken.io/"):
                services.append((ref, value))
            else:
                dependencies.append(value)
        for ref, _ in services:
            self._current(ref)
        if dependencies:
            self.kubectl.apply(yaml.safe_dump_all(dependencies, sort_keys=False))
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
        """Confirm submitted intent still owns the readiness and activation being observed."""
        for ref in self.committed:
            self._current(ref)

    def _current(self, ref: ResourceRef) -> dict[str, Any] | None:
        """Read current service intent and reject operations superseded during preparation."""
        value = self.kubectl.get_if_exists(ref.kind, ref.name, ref.namespace)
        if _intent_version(value) != self.committed.get(ref, self.expected[ref]):
            raise DeploymentError(
                f"DeploymentSuperseded: {ref.display_name} was changed by another operation"
            )
        return value

    def _apply_service(self, ref: ResourceRef, value: dict[str, Any], *, patch: bool = False) -> None:
        """Submit one stage and advance its precondition, retrying only status-update races."""
        deadline = time.monotonic() + self.timeout
        suspended = value["spec"].get("suspend", False)
        while True:
            current = self._current(ref)
            payload = copy.deepcopy(value)
            if patch:
                if current is not None:
                    payload = {
                        "apiVersion": current["apiVersion"], "kind": ref.kind,
                        "metadata": {"name": ref.name, "namespace": ref.namespace},
                        "spec": {"suspend": suspended},
                    }
                # Repeating stop must supersede older preparation without requesting
                # a different executable application through deploymentRevision.
                payload["metadata"].setdefault("annotations", {})[_SERVICE_OPERATION_ANNOTATION] = self.operation
            else:
                payload["metadata"].get("annotations", {}).pop(_SERVICE_OPERATION_ANNOTATION, None)
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
                        ["patch", ref.kind, ref.name, "--namespace", ref.namespace,
                         "--type=merge", "--field-manager=kubectl", "--output=json",
                         "--patch", json.dumps(payload)],
                        timeout=remaining,
                    )
                else:
                    applied = self.kubectl.run(
                        ["apply", "--server-side", "--filename=-", "--output=json"],
                        input_text=yaml.safe_dump(payload, sort_keys=False),
                        timeout=remaining,
                    )
                result = json.loads(applied.stdout)
                if patch and result.get("spec", {}).get("suspend") is not suspended:
                    raise DeploymentError("The platform does not support service suspension; update the platform first")
                self.committed[ref] = _intent_version(result)
                return
            except KubernetesVersionConflictError:
                latest = self._current(ref)
                if current is None or latest["metadata"]["resourceVersion"] == current["metadata"]["resourceVersion"]:
                    # Field ownership conflicts are not optimistic-version races.
                    raise
