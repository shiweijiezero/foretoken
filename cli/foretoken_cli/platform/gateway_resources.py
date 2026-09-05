# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Gateway API resource discovery and ownership checks."""

from __future__ import annotations

import time
from typing import Any

from foretoken_cli.kubernetes import Kubectl, timeout_seconds
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.platform.types import ReleaseRef

def _gateway_class_accepted(value: dict[str, Any]) -> bool:
    """Return whether a GatewayClass is accepted for its current generation."""
    metadata = value.get("metadata") or {}
    generation = int(metadata.get("generation") or 0)
    conditions = (value.get("status") or {}).get("conditions") or []
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Accepted"
        and condition.get("status") == "True"
        and int(condition.get("observedGeneration") or 0) == generation
        for condition in conditions
    )
def _gateway_classes_for_controller(
    kubectl: Kubectl, controller_name: str
) -> tuple[dict[str, Any], ...]:
    """Return GatewayClasses owned by one controller identity."""
    supported = set(kubectl.api_resource_names("gateway.networking.k8s.io"))
    if "gatewayclasses.gateway.networking.k8s.io" not in supported:
        return ()
    return tuple(
        value
        for value in kubectl.list_cluster_resources(
            ("gatewayclasses.gateway.networking.k8s.io",)
        )
        if (value.get("spec") or {}).get("controllerName") == controller_name
    )
def _discover_gateway_class(
    kubectl: Kubectl, controller_name: str
) -> ResourceRef | None:
    """Return one accepted GatewayClass for a controller, when available."""
    accepted = tuple(
        value
        for value in _gateway_classes_for_controller(kubectl, controller_name)
        if _gateway_class_accepted(value)
    )
    if not accepted:
        return None
    value = min(
        accepted,
        key=lambda item: str((item.get("metadata") or {}).get("name") or ""),
    )
    metadata = value.get("metadata") or {}
    return ResourceRef("GatewayClass", str(metadata.get("name") or ""), "")
def _validate_reused_gateway(
    kubectl: Kubectl,
    name: str,
    namespace: str,
    section_name: str,
) -> None:
    """Require an existing Gateway to use an accepted class and listener."""
    gateway = kubectl.get("gateway.gateway.networking.k8s.io", name, namespace)
    gateway_class_name = str((gateway.get("spec") or {}).get("gatewayClassName") or "")
    if not gateway_class_name:
        raise DeploymentError(f"Gateway {namespace}/{name} has no gatewayClassName")
    gateway_class = kubectl.get(
        "gatewayclass.gateway.networking.k8s.io", gateway_class_name
    )
    if not _gateway_class_accepted(gateway_class):
        raise DeploymentError(
            f"GatewayClass {gateway_class_name} is not Accepted by its controller"
        )
    if not section_name:
        return
    listeners = (gateway.get("spec") or {}).get("listeners") or []
    if not any(
        isinstance(listener, dict) and listener.get("name") == section_name
        for listener in listeners
    ):
        raise DeploymentError(
            f"Gateway {namespace}/{name} has no listener named {section_name}"
        )
def _release_owns(metadata: dict[str, Any], release: ReleaseRef) -> bool:
    """Return whether Helm metadata assigns a resource to one release."""
    annotations = metadata.get("annotations") or {}
    return isinstance(annotations, dict) and (
        annotations.get("meta.helm.sh/release-name") == release.name
        and annotations.get("meta.helm.sh/release-namespace") == release.namespace
    )
def _other_controller_gateway_classes(
    kubectl: Kubectl,
    controller_name: str,
    *,
    exclude_release: ReleaseRef | None,
) -> tuple[ResourceRef, ...]:
    """Return GatewayClasses outside an optional platform Helm release."""
    resources: list[ResourceRef] = []
    for value in _gateway_classes_for_controller(kubectl, controller_name):
        metadata = value.get("metadata") or {}
        if exclude_release is not None and _release_owns(metadata, exclude_release):
            continue
        name = str(metadata.get("name") or "")
        if name:
            resources.append(ResourceRef("GatewayClass", name, ""))
    return tuple(resources)
def _external_platform_gateways(
    kubectl: Kubectl,
    platform: ReleaseRef,
    platform_labels: tuple[tuple[str, str], ...],
) -> tuple[ResourceRef, ...]:
    """Return external Gateways that depend on the platform-owned GatewayClass."""
    selector = ",".join(f"{key}={value}" for key, value in platform_labels)
    gateway_classes = kubectl.list_cluster_resources(
        ("gatewayclasses.gateway.networking.k8s.io",),
        label_selector=selector,
    )
    class_names = {
        str((value.get("metadata") or {}).get("name") or "")
        for value in gateway_classes
    }
    class_names.discard("")
    if not class_names:
        return ()

    resources: list[ResourceRef] = []
    for value in kubectl.list_all_resources(
        ("gateways.gateway.networking.k8s.io",)
    ):
        gateway_class_name = str(
            (value.get("spec") or {}).get("gatewayClassName") or ""
        )
        if gateway_class_name not in class_names:
            continue
        metadata = value.get("metadata") or {}
        if _release_owns(metadata, platform):
            continue
        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if name:
            resources.append(ResourceRef("Gateway", name, namespace))
    return tuple(resources)
def _require_no_external_platform_gateways(
    kubectl: Kubectl,
    platform: ReleaseRef,
    platform_labels: tuple[tuple[str, str], ...],
) -> None:
    """Refuse to remove a GatewayClass still used outside its Helm release."""
    users = _external_platform_gateways(kubectl, platform, platform_labels)
    if not users:
        return
    names = ", ".join(
        f"{user.namespace}/{user.display_name}" for user in users
    )
    raise DeploymentError(
        "Foretoken GatewayClass is still used by external Gateways; "
        f"migrate or delete them before changing the platform: {names}"
    )
def _wait_foretoken_gateway_class(
    kubectl: Kubectl,
    controller_name: str,
    platform_labels: tuple[tuple[str, str], ...],
    timeout: str,
) -> ResourceRef:
    """Wait for the Foretoken-owned GatewayClass to become Accepted."""
    deadline = time.monotonic() + timeout_seconds(timeout)
    selector = ",".join(f"{key}={value}" for key, value in platform_labels)
    while time.monotonic() < deadline:
        values = tuple(
            value
            for value in kubectl.list_cluster_resources(
                ("gatewayclasses.gateway.networking.k8s.io",),
                label_selector=selector,
            )
            if (value.get("spec") or {}).get("controllerName")
            == controller_name
        )
        accepted = next(
            (value for value in values if _gateway_class_accepted(value)), None
        )
        if accepted is not None:
            metadata = accepted.get("metadata") or {}
            return ResourceRef(
                "GatewayClass", str(metadata.get("name") or ""), ""
            )
        time.sleep(1)
    raise DeploymentError(
        f"Foretoken GatewayClass was not Accepted within {timeout}; "
        f"selector={selector}"
    )
