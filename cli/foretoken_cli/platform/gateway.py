# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Gateway Controller discovery and lifecycle for platform installation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from foretoken_cli.arguments import InstallCommand
from foretoken_cli.platform.helm import Helm, PlatformGatewayConfig, ReleaseRef
from foretoken_cli.kubernetes import Kubectl, timeout_seconds
from foretoken_cli.manifest import DeploymentError, ResourceRef

@dataclass(frozen=True)
class GatewayControllerPlan:
    """One resolved Gateway Controller lifecycle decision."""

    release: ReleaseRef
    action: str
    detail: str
    controller_name: str = ""
    install: bool = False
    remove: bool = False
    managed: bool = False


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


def _effective_gateway_config(
    command: InstallCommand,
    stored: PlatformGatewayConfig,
) -> PlatformGatewayConfig:
    """Resolve Gateway mode from explicit input or stored release values."""
    if command.frontend_mode is None:
        return stored
    create = command.frontend_mode == "gateway" and not command.gateway_name
    controller_name = stored.controller_name if stored.create else ""
    return PlatformGatewayConfig(
        command.frontend_mode,
        create,
        controller_name,
        command.gateway_name,
        command.gateway_namespace,
        command.gateway_section_name,
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


class GatewayControllerLifecycle:
    """Own Envoy Gateway discovery, installation, readiness, and cleanup."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl

    def _managed_release(self) -> ReleaseRef | None:
        """Return the single CLI-managed Envoy Gateway release, when present."""
        releases = self._helm.managed_envoy_gateway_releases()
        if len(releases) > 1:
            names = ", ".join(release.display_name for release in releases)
            raise DeploymentError(
                f"multiple CLI-managed Envoy Gateway releases exist: {names}"
            )
        return releases[0] if releases else None

    def resolve_install(
        self,
        command: InstallCommand,
        platform: ReleaseRef,
        platform_exists: bool,
    ) -> tuple[PlatformGatewayConfig, GatewayControllerPlan]:
        """Resolve the Gateway mode and Controller lifecycle before installation."""
        helm = self._helm
        kubectl = self._kubectl
        stored_config = (
            helm.platform_gateway_config(platform)
            if platform_exists
            else PlatformGatewayConfig("local", False, "", "", "", "")
        )
        config = _effective_gateway_config(command, stored_config)
        removes_platform_gateway = (
            stored_config.mode == "gateway"
            and stored_config.create
            and not (config.mode == "gateway" and config.create)
        )
        if removes_platform_gateway:
            _require_no_external_platform_gateways(
                kubectl, platform, helm.platform_selector_labels
            )
        release = helm.envoy_gateway_release()
        release_exists = helm.release_exists(release)
        managed_release = self._managed_release()
        release_managed = managed_release == release
        managed_controller = helm.envoy_gateway_controller

        controller_release = (
            managed_release
            if managed_release is not None
            and (
                release_managed
                or config.controller_name == managed_controller
            )
            else None
        )
        if config.mode != "gateway":
            users = (
                _other_controller_gateway_classes(
                    kubectl,
                    managed_controller,
                    exclude_release=platform if platform_exists else None,
                )
                if controller_release is not None
                else ()
            )
            if controller_release is not None and not users:
                return config, GatewayControllerPlan(
                    controller_release,
                    "Remove",
                    controller_release.display_name,
                    managed_controller,
                    remove=True,
                    managed=True,
                )
            if controller_release is not None:
                return config, GatewayControllerPlan(
                    controller_release,
                    "Preserve",
                    controller_release.display_name,
                    managed_controller,
                    managed=True,
                )
            return config, GatewayControllerPlan(
                release,
                "Preserve" if release_exists else "Skip",
                release.display_name if release_exists else "frontend mode is local",
            )

        if not config.create:
            _validate_reused_gateway(
                kubectl, config.name, config.namespace, config.section_name
            )
            users = (
                _other_controller_gateway_classes(
                    kubectl,
                    managed_controller,
                    exclude_release=platform if platform_exists else None,
                )
                if controller_release is not None
                else ()
            )
            return config, GatewayControllerPlan(
                controller_release or release,
                "Reuse",
                f"{config.namespace}/{config.name}",
                managed_controller if controller_release is not None else "",
                remove=controller_release is not None and not users,
                managed=controller_release is not None,
            )

        shared_managed_release = (
            managed_release if managed_release != release else None
        )
        preferred_controller = (
            config.controller_name or helm.envoy_gateway_default_controller
        )
        external_gateway_class = None
        if (
            not release_managed
            and shared_managed_release is None
            and preferred_controller != managed_controller
        ):
            external_gateway_class = _discover_gateway_class(
                kubectl, preferred_controller
            )

        if release_managed:
            return config, GatewayControllerPlan(
                release,
                "Upgrade",
                release.display_name,
                managed_controller,
                install=True,
                managed=True,
            )
        if shared_managed_release is not None:
            return config, GatewayControllerPlan(
                shared_managed_release,
                "Reuse",
                shared_managed_release.display_name,
                managed_controller,
                managed=True,
            )
        if external_gateway_class is not None:
            return config, GatewayControllerPlan(
                release,
                "Reuse",
                external_gateway_class.display_name,
                preferred_controller,
            )
        if release_exists:
            raise DeploymentError(
                f"Helm release {release.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )

        reserved_classes = _gateway_classes_for_controller(kubectl, managed_controller)
        recovering_release = (
            platform_exists and config.controller_name == managed_controller
        )
        if reserved_classes and not recovering_release:
            classes = ", ".join(
                str((value.get("metadata") or {}).get("name") or "")
                for value in reserved_classes
            )
            raise DeploymentError(
                "reserved Foretoken GatewayClasses exist without a CLI-managed "
                f"Envoy Gateway release: {classes}"
            )
        return config, GatewayControllerPlan(
            release,
            "Install",
            release.display_name,
            managed_controller,
            install=True,
            managed=True,
        )

    def apply_before_platform(
        self, plan: GatewayControllerPlan, timeout: str, dry_run: bool
    ) -> None:
        """Install or update a managed Controller before the platform release."""
        helm = self._helm
        if plan.install:
            helm.install_envoy_gateway(plan.release, timeout, dry_run)

    def finish_update(
        self,
        plan: GatewayControllerPlan,
        config: PlatformGatewayConfig,
        timeout: str,
    ) -> tuple[tuple[str, str, str], ...]:
        """Finish Controller cleanup and GatewayClass readiness after update."""
        helm = self._helm
        kubectl = self._kubectl
        results: list[tuple[str, str, str]] = []
        if plan.remove:
            remaining = _other_controller_gateway_classes(
                kubectl, plan.controller_name, exclude_release=None
            )
            if remaining:
                users = ", ".join(user.display_name for user in remaining)
                results.append(("Gateway Controller", "Preserve", users))
            else:
                helm.uninstall(plan.release, timeout)
                results.append(
                    ("Gateway Controller", "Removed", plan.release.display_name)
                )
        if plan.install:
            results.append(("Gateway Controller", "Ready", plan.release.display_name))
        if config.mode == "gateway" and config.create:
            gateway_class = _wait_foretoken_gateway_class(
                kubectl,
                plan.controller_name,
                helm.platform_selector_labels,
                timeout,
            )
            results.append(("GatewayClass", "Ready", gateway_class.display_name))
        return tuple(results)

    def resolve_uninstall(
        self, platform: ReleaseRef, *, platform_exists: bool
    ) -> GatewayControllerPlan:
        """Resolve Controller removal for one platform release."""
        helm = self._helm
        kubectl = self._kubectl
        release = helm.envoy_gateway_release()
        release_exists = helm.release_exists(release)
        managed_release = self._managed_release()
        controller_name = helm.envoy_gateway_controller
        config = (
            helm.platform_gateway_config(platform)
            if platform_exists
            else PlatformGatewayConfig("local", False, "", "", "", "")
        )
        if platform_exists and config.mode == "gateway" and config.create:
            _require_no_external_platform_gateways(
                kubectl, platform, helm.platform_selector_labels
            )
        controller_release = (
            managed_release
            if managed_release is not None
            and (
                managed_release == release
                or config.controller_name == controller_name
            )
            else None
        )
        users = (
            _other_controller_gateway_classes(
                kubectl,
                controller_name,
                exclude_release=platform if platform_exists else None,
            )
            if controller_release is not None
            else ()
        )
        if controller_release is not None and not users:
            return GatewayControllerPlan(
                controller_release,
                "Remove",
                controller_release.display_name,
                controller_name,
                remove=True,
                managed=True,
            )
        if controller_release is not None:
            return GatewayControllerPlan(
                controller_release,
                "Preserve",
                ", ".join(user.display_name for user in users),
                controller_name,
                managed=True,
            )
        return GatewayControllerPlan(
            release,
            "Preserve" if release_exists else "Skip",
            release.display_name if release_exists else "no managed release",
        )

    def finish_uninstall(
        self, plan: GatewayControllerPlan, timeout: str
    ) -> tuple[str, str] | None:
        """Remove a managed Controller only after its final GatewayClass is gone."""
        helm = self._helm
        kubectl = self._kubectl
        if not plan.managed:
            return None
        remaining = _other_controller_gateway_classes(
            kubectl, plan.controller_name, exclude_release=None
        )
        if remaining:
            return "Preserve", ", ".join(user.display_name for user in remaining)
        helm.uninstall(plan.release, timeout)
        return "Removed", plan.release.display_name
