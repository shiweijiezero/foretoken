# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Installation lifecycle for the Foretoken platform and managed dependencies."""

from __future__ import annotations

from foretoken_cli.accelerators.discovery import ExporterDiscovery
from foretoken_cli.accelerators.metax import MetaXMetricsDiscovery
from foretoken_cli.accelerators.nvidia import NvidiaMetricsDiscovery
from foretoken_cli.arguments import InstallCommand, UninstallCommand
from foretoken_cli.kubernetes import (
    Kubectl,
    control_plane_deployments,
    mark_managed_metrics_scraper_namespace,
    platform_service_resources,
    unmark_managed_metrics_scraper_namespace,
)
from foretoken_cli.manifest import DeploymentError
from foretoken_cli.observability import PrometheusRef, select_prometheus
from foretoken_cli.platform.config import default_platform_config
from foretoken_cli.platform.gateway import GatewayControllerLifecycle
from foretoken_cli.platform.helm import Helm
from foretoken_cli.source import (
    prepare_source_images,
    restart_changed_source_deployments,
)


def _print_plan(responsibility: str, action: str, detail: str) -> None:
    """Print one stable installation lifecycle decision."""
    print(f"{responsibility:<28} {action:<8} {detail}")


class PlatformLifecycle:
    """Own platform installation and managed dependency lifecycles."""

    def __init__(self) -> None:
        self._helm = Helm(default_platform_config())
        self._kubectl = Kubectl()
        self._gateway = GatewayControllerLifecycle(self._helm, self._kubectl)

    def install(self, command: InstallCommand) -> None:
        """Install managed dependencies and update the Foretoken platform release."""
        helm = self._helm
        kubectl = self._kubectl
        gateway = self._gateway

        platform = helm.platform_release()
        platform_exists = helm.release_exists(platform)
        if platform_exists and not helm.is_cli_managed(platform):
            raise DeploymentError(
                f"Helm release {platform.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )
        if platform_exists:
            install_source = helm.release_install_source(platform)
            requested_source = "source" if command.editable is not None else "release"
            if install_source != requested_source:
                command_hint = (
                    "foretoken install -e PATH"
                    if install_source == "source"
                    else "foretoken install"
                )
                raise DeploymentError(
                    f"Helm release {platform.display_name} uses {install_source} images; "
                    f"run {command_hint}"
                )
        helm.validate_platform_values(command.values)

        deployments = control_plane_deployments(kubectl)
        expected_deployment = f"{platform.name}-control-plane"
        unexpected_deployments = tuple(
            deployment
            for deployment in deployments
            if not (
                platform_exists
                and deployment.namespace == platform.namespace
                and deployment.name == expected_deployment
            )
        )
        if unexpected_deployments:
            existing = ", ".join(
                f"{deployment.namespace}/{deployment.display_name}"
                for deployment in unexpected_deployments
            )
            raise DeploymentError(
                "another Foretoken control plane already exists; use its existing "
                f"lifecycle: {existing}"
            )

        gateway_config, gateway_plan = gateway.resolve_install(
            command, platform, platform_exists
        )

        managed_dcgm = helm.dcgm_release()
        managed_dcgm_exists = helm.release_exists(managed_dcgm)
        if managed_dcgm_exists and not helm.is_cli_managed(managed_dcgm):
            raise DeploymentError(
                f"Helm release {managed_dcgm.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )
        exporter_discovery = ExporterDiscovery(kubectl)
        nvidia_metrics = NvidiaMetricsDiscovery(exporter_discovery).resolve(
            managed_dcgm if managed_dcgm_exists else None
        )
        metax_metrics = MetaXMetricsDiscovery(exporter_discovery).resolve()

        managed_prometheus = helm.prometheus_release()
        managed_prometheus_exists = helm.release_exists(managed_prometheus)
        selected_prometheus: PrometheusRef | None = None
        if managed_prometheus_exists:
            if not helm.is_cli_managed(managed_prometheus):
                raise DeploymentError(
                    f"Helm release {managed_prometheus.display_name} is not managed by "
                    "foretoken; use its existing Helm lifecycle"
                )
            prometheus_action = "Upgrade"
            prometheus_detail = managed_prometheus.display_name
        else:
            selected_prometheus = select_prometheus(
                kubectl, platform.namespace, command.prometheus
            )
            if selected_prometheus is None:
                prometheus_action = "Install"
                prometheus_detail = managed_prometheus.display_name
            else:
                prometheus_action = "Reuse"
                prometheus_detail = (
                    f"{selected_prometheus.namespace}/{selected_prometheus.name}"
                )

        install_managed_prometheus = (
            managed_prometheus_exists or selected_prometheus is None
        )
        exporters = tuple(
            (name, exporter)
            for name, exporter in (
                (
                    "DCGM",
                    nvidia_metrics.exporter if nvidia_metrics is not None else None,
                ),
                ("mxExporter", metax_metrics),
            )
            if exporter is not None
        )
        if selected_prometheus is not None:
            exporter_discovery.require_prometheus_selection(
                selected_prometheus, exporters
            )
        if nvidia_metrics is None:
            nvidia_action = "Preserve" if managed_dcgm_exists else "Skip"
            nvidia_detail = (
                managed_dcgm.display_name
                if managed_dcgm_exists
                else "no allocatable nvidia.com/gpu resource"
            )
            install_managed_dcgm = False
        elif managed_dcgm_exists:
            nvidia_action = "Upgrade"
            nvidia_detail = managed_dcgm.display_name
            install_managed_dcgm = True
        elif nvidia_metrics.exporter is not None:
            nvidia_action = "Reuse"
            nvidia_detail = (
                f"{nvidia_metrics.exporter.daemonset.namespace}/"
                f"{nvidia_metrics.exporter.daemonset.display_name}"
            )
            install_managed_dcgm = False
        else:
            nvidia_action = "Install"
            nvidia_detail = managed_dcgm.display_name
            install_managed_dcgm = True

        if metax_metrics is None:
            metax_action = "Skip"
            metax_detail = "no allocatable metax-tech.com/gpu resource"
        else:
            metax_action = "Reuse"
            metax_detail = (
                f"{metax_metrics.daemonset.namespace}/"
                f"{metax_metrics.daemonset.display_name}"
            )

        observability_labels = (
            () if selected_prometheus is None else selected_prometheus.additional_labels
        )
        monitor_namespaces = {
            platform.namespace,
            *(exporter.service_monitor.namespace for _, exporter in exporters),
        }

        platform_action = "Upgrade" if platform_exists else "Install"
        if command.editable is not None:
            _print_plan("Source images", "Build", command.editable)
        _print_plan(
            "Gateway Controller", gateway_plan.action, gateway_plan.detail
        )
        _print_plan("Prometheus", prometheus_action, prometheus_detail)
        _print_plan("NVIDIA DCGM Exporter", nvidia_action, nvidia_detail)
        _print_plan("MetaX mxExporter", metax_action, metax_detail)
        _print_plan("Foretoken platform", platform_action, platform.display_name)

        source_images = (
            prepare_source_images(
                command.editable,
                command.registry,
                platform.namespace,
                command.timeout,
                command.dry_run,
            )
            if command.editable is not None
            else None
        )
        gateway.apply_before_platform(
            gateway_plan, command.timeout, command.dry_run
        )
        if install_managed_prometheus:
            helm.install_prometheus(
                managed_prometheus,
                tuple(sorted(monitor_namespaces)),
                command.timeout,
                command.dry_run,
            )
        if install_managed_dcgm:
            helm.install_dcgm_exporter(
                managed_dcgm,
                observability_labels,
                nvidia_metrics.node_selector,
                managed_dcgm_exists,
                command.timeout,
                command.dry_run,
            )
        dry_run_api_versions: list[str] = []
        if command.dry_run and install_managed_prometheus:
            dry_run_api_versions.extend(
                (
                    "monitoring.coreos.com/v1/ServiceMonitor",
                    "monitoring.coreos.com/v1/PrometheusRule",
                )
            )
        if command.dry_run and gateway_plan.install:
            dry_run_api_versions.extend(
                (
                    "gateway.networking.k8s.io/v1/GatewayClass",
                    "gateway.networking.k8s.io/v1/Gateway",
                    "gateway.networking.k8s.io/v1/HTTPRoute",
                )
            )
        platform_frontend_mode = command.frontend_mode
        platform_gateway_name = command.gateway_name
        platform_gateway_namespace = command.gateway_namespace
        platform_gateway_section_name = command.gateway_section_name
        if command.dry_run and dry_run_api_versions:
            platform_frontend_mode = gateway_config.mode
            platform_gateway_name = "" if gateway_config.create else gateway_config.name
            platform_gateway_namespace = (
                "" if gateway_config.create else gateway_config.namespace
            )
            platform_gateway_section_name = (
                "" if gateway_config.create else gateway_config.section_name
            )
        helm.install_platform(
            release=platform,
            source_images=source_images,
            values=command.values,
            frontend_mode=platform_frontend_mode,
            gateway_name=platform_gateway_name,
            gateway_namespace=platform_gateway_namespace,
            gateway_section_name=platform_gateway_section_name,
            gateway_controller_name=gateway_plan.controller_name,
            observability_labels=observability_labels,
            reuse_values=platform_exists,
            timeout=command.timeout,
            dry_run=command.dry_run,
            dry_run_api_versions=tuple(dry_run_api_versions),
        )
        if not command.dry_run:
            if source_images is not None:
                restart_changed_source_deployments(
                    kubectl,
                    source_images,
                    platform.namespace,
                    command.timeout,
                )
            for responsibility, action, detail in gateway.finish_update(
                gateway_plan, gateway_config, command.timeout
            ):
                _print_plan(responsibility, action, detail)
            if install_managed_prometheus:
                mark_managed_metrics_scraper_namespace(
                    kubectl, managed_prometheus.namespace
                )
                _print_plan("Prometheus", "Ready", managed_prometheus.display_name)
            if install_managed_dcgm:
                _print_plan("NVIDIA DCGM Exporter", "Ready", managed_dcgm.display_name)
            _print_plan("Foretoken platform", "Ready", platform.display_name)

    def uninstall(self, command: UninstallCommand) -> None:
        """Remove CLI-owned releases after user services are gone."""
        helm = self._helm
        kubectl = self._kubectl
        gateway = self._gateway
        platform = helm.platform_release()
        managed_dcgm = helm.dcgm_release()
        managed_prometheus = helm.prometheus_release()

        platform_exists = helm.release_exists(platform)
        if platform_exists and not helm.is_cli_managed(platform):
            raise DeploymentError(
                f"Helm release {platform.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )

        dcgm_exists = helm.release_exists(managed_dcgm)
        dcgm_managed = dcgm_exists and helm.is_cli_managed(managed_dcgm)
        prometheus_exists = helm.release_exists(managed_prometheus)
        prometheus_managed = (
            prometheus_exists and helm.is_cli_managed(managed_prometheus)
        )
        gateway_plan = gateway.resolve_uninstall(
            platform, platform_exists=platform_exists
        )
        if (
            platform_exists
            or dcgm_managed
            or prometheus_managed
            or gateway_plan.managed
        ):
            resources = platform_service_resources(kubectl)
            if resources:
                remaining = ", ".join(
                    f"{resource.namespace}/{resource.display_name}"
                    for resource in resources
                )
                raise DeploymentError(
                    "delete Foretoken services before uninstalling the platform: "
                    f"{remaining}"
                )

        if platform_exists:
            _print_plan("Foretoken platform", "Remove", platform.display_name)
        else:
            _print_plan("Foretoken platform", "Skip", "not installed")
        if dcgm_managed:
            _print_plan("NVIDIA DCGM Exporter", "Remove", managed_dcgm.display_name)
        elif dcgm_exists:
            _print_plan("NVIDIA DCGM Exporter", "Preserve", managed_dcgm.display_name)
        else:
            _print_plan("NVIDIA DCGM Exporter", "Skip", "no managed release")
        if prometheus_managed:
            _print_plan("Prometheus", "Remove", managed_prometheus.display_name)
        elif prometheus_exists:
            _print_plan("Prometheus", "Preserve", managed_prometheus.display_name)
        else:
            _print_plan("Prometheus", "Skip", "no managed release")
        _print_plan(
            "Gateway Controller", gateway_plan.action, gateway_plan.detail
        )
        if command.dry_run:
            return

        if platform_exists:
            helm.uninstall(platform, command.timeout)
            _print_plan("Foretoken platform", "Removed", platform.display_name)
        if dcgm_managed:
            helm.uninstall(managed_dcgm, command.timeout)
            _print_plan("NVIDIA DCGM Exporter", "Removed", managed_dcgm.display_name)
        if prometheus_managed:
            helm.uninstall(managed_prometheus, command.timeout)
            unmark_managed_metrics_scraper_namespace(
                kubectl, managed_prometheus.namespace
            )
            _print_plan("Prometheus", "Removed", managed_prometheus.display_name)
        gateway_result = gateway.finish_uninstall(
            gateway_plan, command.timeout
        )
        if gateway_result is not None:
            _print_plan("Gateway Controller", *gateway_result)
