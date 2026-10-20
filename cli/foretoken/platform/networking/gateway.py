# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Gateway Controller discovery and lifecycle for platform installation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yaml

from foretoken.arguments import InstallCommand
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.network_sources import select_platform_image_sources
from foretoken.platform.helm_client import HelmClient, value_at
from foretoken.platform.networking.gateway_resources import (
    _discover_gateway_class,
    _gateway_classes_for_controller,
    _other_controller_gateway_classes,
    _require_no_external_platform_gateways,
    _validate_reused_gateway,
    _wait_foretoken_gateway_class,
)
from foretoken.platform.types import PlatformGatewayConfig, ReleaseRef


@dataclass(frozen=True)
class GatewayControllerPlan:
    """One resolved Gateway Controller lifecycle decision."""

    release: ReleaseRef
    action: str
    detail: str
    controller_name: str = ""
    managed: bool = False

    @property
    def install(self) -> bool:
        """Return whether the plan installs or upgrades its Controller release."""
        return self.action in {"Install", "Upgrade"}

    @property
    def remove(self) -> bool:
        """Return whether the plan removes its Controller release."""
        return self.action == "Remove"


class GatewayControllerLifecycle:
    """Own Envoy Gateway discovery, installation, readiness, and cleanup."""

    def __init__(self, helm: HelmClient, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl
        self._release = ReleaseRef(helm.config.envoy_gateway.release_name, helm.config.namespace)

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
            self._platform_gateway_config(platform)
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
                kubectl, platform, helm.config.platform_selector_labels
            )
        release = self._release
        release_exists = helm.release_exists(release)
        managed_release = self._managed_release()
        release_managed = managed_release == release
        managed_controller = helm.config.envoy_gateway_controller

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
            return config, GatewayControllerPlan(
                controller_release or release,
                "Reuse",
                f"{config.namespace}/{config.name}",
                managed_controller if controller_release is not None else "",
                managed=controller_release is not None,
            )

        shared_managed_release = (
            managed_release if managed_release != release else None
        )
        preferred_controller = (
            config.controller_name or helm.config.envoy_gateway_default_controller
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
            managed=True,
        )

    def apply_before_platform(
        self, plan: GatewayControllerPlan, timeout: str
    ) -> None:
        """Install or update a managed Controller before the platform release."""
        if not plan.install:
            return
        helm = self._helm
        chart = helm.config.envoy_gateway
        args = helm.upgrade_args(plan.release, chart.source, timeout)
        args.extend([
            "--set-string",
            "config.envoyGateway.gateway.controllerName=" + helm.config.envoy_gateway_controller,
        ])
        values = helm.stored_chart_values(plan.release)
        input_text = json.dumps(values)
        args.extend(["--reset-values", "--values", "-"])
        with helm.prepared_chart(chart.source, chart.version) as prepared:
            args[3] = str(prepared)
            # The chart helper owns the proxy version independently of Gateway's version.
            helm.set_chart_defaults(prepared, {"global.images.envoyProxy.image": "docker.io/envoyproxy/envoy"})
            images = self._envoy_gateway_images(helm.render_chart(args, input_text=input_text))
            previous = self._envoy_gateway_images(helm.installed_chart_manifest(plan.release))
            references = {}
            prior_sources = {}
            for role in ("envoyGateway", "envoyProxy", "ratelimit"):
                path = f"global.images.{role}.image"
                try:
                    if value_at(values, path):
                        continue
                except KeyError:
                    pass
                references[path] = images[role]
                if role in previous:
                    prior_sources[path] = previous[role]
            sources = select_platform_image_sources(references, helm.config.image_registry, previous=prior_sources)
            helm.set_chart_defaults(prepared, sources)
            helm.run(args, input_text=input_text)

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
                helm.config.platform_selector_labels,
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
        release = self._release
        release_exists = helm.release_exists(release)
        managed_release = self._managed_release(include_legacy=True)
        controller_name = helm.config.envoy_gateway_controller
        config = (
            self._platform_gateway_config(platform)
            if platform_exists
            else PlatformGatewayConfig("local", False, "", "", "", "")
        )
        if platform_exists and config.mode == "gateway" and config.create:
            _require_no_external_platform_gateways(
                kubectl, platform, helm.config.platform_selector_labels
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

    def _managed_release(self, *, include_legacy: bool = False) -> ReleaseRef | None:
        """Return one managed Envoy Gateway release, when present."""
        labels = (self._helm.config.management_label,)
        if include_legacy:
            labels += (self._helm.config.legacy_management_label,)
        releases: dict[tuple[str, str], ReleaseRef] = {}
        for label in labels:
            for item in self._helm.list_releases(
                self._release.name, namespace=None, selector="=".join(label),
            ):
                if item.get("name") and item.get("namespace"):
                    release = ReleaseRef(str(item["name"]), str(item["namespace"]))
                    releases[(release.namespace, release.name)] = release
        if len(releases) > 1:
            names = ", ".join(release.display_name for release in releases.values())
            raise DeploymentError(f"multiple CLI-managed Envoy Gateway releases exist: {names}")
        return next(iter(releases.values()), None)

    def _platform_gateway_config(self, release: ReleaseRef) -> PlatformGatewayConfig:
        """Read the native Gateway choice retained by an installed platform release."""
        frontend = self._helm.release_values(release).get("frontend") or {}
        if not isinstance(frontend, dict):
            raise DeploymentError("platform Gateway values are invalid")
        gateway = frontend.get("gateway") or {}
        if not isinstance(gateway, dict):
            raise DeploymentError("platform Gateway values are invalid")
        return PlatformGatewayConfig(
            mode=str(frontend.get("mode") or "local"), create=bool(gateway.get("create")),
            controller_name=str(gateway.get("controllerName") or ""),
            name=str(gateway.get("name") or ""), namespace=str(gateway.get("namespace") or ""),
            section_name=str(gateway.get("sectionName") or ""),
        )

    @staticmethod
    def _envoy_gateway_images(documents: tuple[dict[str, Any], ...]) -> dict[str, str]:
        """Read the native controller and proxy image choices from Envoy resources."""
        images = {}
        for document in documents:
            if document["kind"] == "Deployment":
                containers = document["spec"]["template"]["spec"]["containers"]
                images["envoyGateway"] = containers[0]["image"]
            if document["kind"] == "ConfigMap" and "envoy-gateway.yaml" in document.get("data", {}):
                config = yaml.safe_load(document["data"]["envoy-gateway.yaml"])
                for role, path in (
                    ("envoyProxy", "envoyProxy.provider.kubernetes.envoyDeployment.container.image"),
                    ("ratelimit", "provider.kubernetes.rateLimitDeployment.container.image"),
                ):
                    images[role] = value_at(config, path)
        return images


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
