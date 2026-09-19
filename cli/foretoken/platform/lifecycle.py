# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Installation lifecycle for the Foretoken platform and managed dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken.accelerators.config import (
    GPU_RESOURCE_BACKENDS,
    METAX_GPU_RESOURCES,
    NVIDIA_GPU_RESOURCE,
)
from foretoken.accelerators.discovery import ExporterDiscovery
from foretoken.accelerators.metax import MetaXExporterLifecycle, MetaXMetricsDiscovery
from foretoken.accelerators.nvidia import NvidiaMetricsDiscovery
from foretoken.arguments import InstallCommand, UninstallCommand
from foretoken.kubernetes import (
    Kubectl,
    control_plane_deployments,
    mark_managed_metrics_scraper_namespace,
    platform_service_resources,
    timeout_seconds,
    unmark_managed_metrics_scraper_namespace,
)
from foretoken.manifest import DeploymentError
from foretoken.observability import PrometheusRef, select_prometheus
from foretoken.platform.config import (
    default_platform_config,
    load_platform_values,
    resolve_load_balancer_config,
    runtime_overrides_from_values,
)
from foretoken.platform.gateway import GatewayControllerLifecycle
from foretoken.platform.helm import Helm
from foretoken.platform.leader_worker import LeaderWorkerLifecycle
from foretoken.platform.load_balancer import LoadBalancerLifecycle
from foretoken.platform.rdma import (
    migrate_stored_rdma_values,
    require_unused_managed_rdma,
    select_rdma,
)
from foretoken.platform.types import RuntimeOverrides
from foretoken.source import (
    prepare_source_images,
    restart_changed_source_deployments,
)


def _print_plan(responsibility: str, action: str, detail: str) -> None:
    """Print one stable installation lifecycle decision."""
    print(f"{responsibility:<28} {action:<20} {detail}")


@dataclass(frozen=True)
class _RuntimeSelection:
    """One accelerator backend and Kubernetes resource selected for the platform."""

    backend: str
    resource_name: str
    nodes: tuple[dict[str, Any], ...]


def _resource_capacity(node: dict[str, Any], resource_name: str) -> int:
    """Return one node's allocatable extended-resource capacity."""
    value = ((node.get("status") or {}).get("allocatable") or {}).get(
        resource_name
    )
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _select_runtime(
    nodes: tuple[dict[str, Any], ...], overrides: RuntimeOverrides
) -> _RuntimeSelection | None:
    """Select the single supported accelerator resource within the configured scope."""
    selected_nodes = nodes
    if overrides.gpu_node_selector is not None:
        key, value = overrides.gpu_node_selector
        if key:
            selected_nodes = tuple(
                node
                for node in nodes
                if ((node.get("metadata") or {}).get("labels") or {}).get(key)
                == value
            )

    if overrides.gpu_resource_name is not None:
        resource_name = overrides.gpu_resource_name
        if not resource_name:
            return None
    else:
        resources = tuple(
            resource
            for resource in GPU_RESOURCE_BACKENDS
            if any(_resource_capacity(node, resource) > 0 for node in selected_nodes)
        )
        if not resources:
            return None
        if len(resources) > 1:
            raise DeploymentError(
                "multiple accelerator resources are allocatable in the selected "
                "cluster scope: "
                + ", ".join(resources)
                + "; set runtime.vllm.gpu.resourceName or runtime.vllm.gpu.nodeSelector "
                "in --values"
            )
        resource_name = resources[0]
    return _RuntimeSelection(
        GPU_RESOURCE_BACKENDS.get(resource_name, "custom"),
        resource_name,
        tuple(
            node for node in selected_nodes
            if _resource_capacity(node, resource_name) > 0
        ),
    )


class PlatformLifecycle:
    """Own platform installation and managed dependency lifecycles."""

    def __init__(self, oci_registry: str | None = None) -> None:
        config = default_platform_config(oci_registry)
        self._helm = Helm(config)
        self._oci_registry = config.image_registry
        self._kubectl = Kubectl()
        self._metax_exporter = MetaXExporterLifecycle(
            self._kubectl, config.management_label, config.metax_exporter_image,
            config.image_registry,
        )
        self._gateway = GatewayControllerLifecycle(self._helm, self._kubectl)
        self._leader_worker = LeaderWorkerLifecycle(self._helm, self._kubectl)
        self._load_balancer = LoadBalancerLifecycle(self._helm, self._kubectl)

    def install(self, command: InstallCommand) -> None:
        """Install managed dependencies and update the Foretoken platform release."""
        helm = self._helm
        kubectl = self._kubectl
        gateway = self._gateway
        load_balancer = self._load_balancer
        metax_exporter = self._metax_exporter
        timeout_seconds(command.timeout)

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
        values = load_platform_values(command.values)
        current_runtime = runtime_overrides_from_values(values)
        stored_values = (
            (migrate_stored_rdma_values(helm.release_user_values(platform)),)
            if platform_exists else ()
        )
        stored_runtime = runtime_overrides_from_values(stored_values)
        runtime_scope = RuntimeOverrides(
            gpu_resource_name=(
                current_runtime.gpu_resource_name
                if current_runtime.gpu_resource_name is not None
                else (
                    None
                    if current_runtime.gpu_node_selector is not None
                    else stored_runtime.gpu_resource_name
                )
            ),
            gpu_node_selector=(
                current_runtime.gpu_node_selector
                if current_runtime.gpu_node_selector is not None
                else stored_runtime.gpu_node_selector
            ),
        )
        load_balancer_config = resolve_load_balancer_config(values)

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

        load_balancer_plan = load_balancer.resolve_install(load_balancer_config)
        if load_balancer_plan.blocking:
            _print_plan(
                "LoadBalancer",
                load_balancer_plan.action,
                load_balancer_plan.detail,
            )
            raise DeploymentError(load_balancer_plan.detail)

        gateway_config, gateway_plan = gateway.resolve_install(
            command, platform, platform_exists
        )
        leader_worker_plan = self._leader_worker.resolve_install()

        managed_dcgm = helm.dcgm_release()
        managed_dcgm_exists = helm.release_exists(managed_dcgm)
        if managed_dcgm_exists and not helm.is_cli_managed(managed_dcgm):
            raise DeploymentError(
                f"Helm release {managed_dcgm.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )
        exporter_discovery = ExporterDiscovery(kubectl, command.timeout)
        runtime_selection = _select_runtime(exporter_discovery.nodes, runtime_scope)
        rdma = select_rdma(
            kubectl,
            runtime_selection.nodes if runtime_selection is not None else (),
            exporter_discovery.daemonsets,
            (*stored_values, *values),
            (platform.name, platform.namespace),
        )

        if platform_exists and (
            not rdma.managed
            or runtime_scope.gpu_node_selector != stored_runtime.gpu_node_selector
            or bool(
                set(stored_values[0].get("rdma", {}).get("nodeNames", []))
                - set(rdma.node_names)
            )
        ):
            require_unused_managed_rdma(kubectl, (platform.name, platform.namespace))

        source_runtime_image: str | None = None
        configured_runtime_image = (
            current_runtime.image
            if current_runtime.image is not None
            else stored_runtime.image
        )
        if command.editable is not None:
            if current_runtime.image not in {None, "auto"}:
                source_runtime_image = current_runtime.image or None
            elif runtime_selection is not None and runtime_selection.backend == "metax":
                source_runtime_image = helm.platform_runtime_image(
                    Path(command.editable).expanduser().resolve(),
                    runtime_selection.resource_name,
                )
            elif (
                runtime_selection is not None
                and runtime_selection.backend == "custom"
            ):
                raise DeploymentError(
                    "runtime.vllm.image must be set in --values for source builds on "
                    f"GPU resource {runtime_selection.resource_name}"
                )
        elif (
            runtime_selection is not None
            and runtime_selection.backend == "custom"
            and configured_runtime_image in {None, "auto"}
        ):
            raise DeploymentError(
                "runtime.vllm.image must be set in --values for GPU resource "
                f"{runtime_selection.resource_name}"
            )

        gpu_resource_name = (
            runtime_selection.resource_name
            if runtime_selection is not None
            and current_runtime.gpu_resource_name is None
            and (
                stored_runtime.gpu_resource_name is None
                or current_runtime.gpu_node_selector is not None
            )
            else None
        )

        nvidia_metrics = NvidiaMetricsDiscovery(exporter_discovery).resolve(
            managed_dcgm if managed_dcgm_exists else None
        )
        managed_metax_exists = bool(metax_exporter.managed_resources())
        metax_metrics = MetaXMetricsDiscovery(exporter_discovery).resolve(
            metax_exporter.daemonset if managed_metax_exists else None
        )
        install_managed_metax = (
            metax_metrics is not None and metax_metrics.exporter is None
        )
        if install_managed_metax:
            metax_exporter.ensure_available()

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
                (
                    "mxExporter",
                    metax_metrics.exporter if metax_metrics is not None else None,
                ),
            )
            if exporter is not None
        )
        if selected_prometheus is not None:
            exporter_discovery.require_prometheus_selection(
                selected_prometheus, exporters
            )
            exporter_discovery.require_prometheus_targets(
                selected_prometheus, exporters
            )
        if nvidia_metrics is None:
            nvidia_action = "Preserve" if managed_dcgm_exists else "Skip"
            nvidia_detail = (
                managed_dcgm.display_name
                if managed_dcgm_exists
                else f"no allocatable {NVIDIA_GPU_RESOURCE} resource"
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
            metax_detail = f"no allocatable {' or '.join(METAX_GPU_RESOURCES)} resource"
        elif install_managed_metax:
            metax_action = "Upgrade" if managed_metax_exists else "Install"
            metax_detail = f"{metax_exporter.namespace}/{metax_exporter.daemonset.name}"
        else:
            metax_action = "Reuse"
            metax_detail = (
                f"{metax_metrics.exporter.daemonset.namespace}/"
                f"{metax_metrics.exporter.daemonset.display_name}"
            )

        observability_labels = (
            () if selected_prometheus is None else selected_prometheus.additional_labels
        )
        monitor_namespaces = {
            platform.namespace,
            *(exporter.service_monitor.namespace for _, exporter in exporters),
            *({metax_exporter.namespace} if install_managed_metax else set()),
        }

        platform_action = "Upgrade" if platform_exists else "Install"
        if command.editable is not None:
            _print_plan("Source images", "Build", command.editable)
        _print_plan(
            "LoadBalancer", load_balancer_plan.action, load_balancer_plan.detail
        )
        _print_plan(
            "Gateway Controller", gateway_plan.action, gateway_plan.detail
        )
        _print_plan("Prometheus", prometheus_action, prometheus_detail)
        _print_plan("NVIDIA DCGM Exporter", nvidia_action, nvidia_detail)
        _print_plan("MetaX mxExporter", metax_action, metax_detail)
        if runtime_selection is None:
            runtime_action = "Default"
            runtime_detail = "chart runtime (no allocatable supported GPU detected)"
        else:
            runtime_action = (
                "Configured"
                if current_runtime.gpu_resource_name is not None
                or current_runtime.gpu_node_selector is not None
                or stored_runtime.gpu_resource_name is not None
                or stored_runtime.gpu_node_selector is not None
                else "Auto-select"
            )
            runtime_detail = (
                f"{runtime_selection.backend} via {runtime_selection.resource_name}"
            )
        _print_plan("Inference runtime", runtime_action, runtime_detail)
        _print_plan("RDMA", rdma.action, rdma.detail)
        _print_plan("LeaderWorkerSet", leader_worker_plan.action, leader_worker_plan.detail)
        _print_plan("Foretoken platform", platform_action, platform.display_name)

        source_images = (
            prepare_source_images(
                command.editable,
                command.registry,
                self._oci_registry,
                platform.namespace,
                command.timeout,
                source_runtime_image,
            )
            if command.editable is not None
            else None
        )
        load_balancer.apply(load_balancer_plan, command.timeout)
        gateway.apply_before_platform(gateway_plan, command.timeout)
        self._leader_worker.apply(leader_worker_plan, command.timeout)
        _print_plan("LeaderWorkerSet", "Ready", leader_worker_plan.detail)
        if install_managed_prometheus:
            helm.install_prometheus(
                managed_prometheus,
                tuple(sorted(monitor_namespaces)),
                command.timeout,
            )
            mark_managed_metrics_scraper_namespace(kubectl, managed_prometheus.namespace)
            resource = helm.prometheus_resource(managed_prometheus)
            selected_prometheus = PrometheusRef(resource.name, resource.namespace, ())
        if install_managed_dcgm:
            helm.install_dcgm_exporter(
                managed_dcgm,
                observability_labels,
                nvidia_metrics.node_selector,
                managed_dcgm_exists,
                command.timeout,
            )
        if install_managed_metax:
            metax_exporter.install(
                metax_metrics.node_names, observability_labels, command.timeout
            )

        if install_managed_prometheus or install_managed_dcgm or install_managed_metax:
            verified_discovery = ExporterDiscovery(kubectl, command.timeout)
            verified_nvidia = NvidiaMetricsDiscovery(verified_discovery).resolve()
            verified_metax = MetaXMetricsDiscovery(verified_discovery).resolve()
            verified_exporters = tuple(
                (name, exporter)
                for name, exporter in (
                    (
                        "DCGM",
                        verified_nvidia.exporter
                        if verified_nvidia is not None
                        else None,
                    ),
                    (
                        "mxExporter",
                        verified_metax.exporter
                        if verified_metax is not None
                        else None,
                    ),
                )
                if exporter is not None
            )
            verified_discovery.require_prometheus_selection(
                selected_prometheus, verified_exporters
            )
            verified_discovery.require_prometheus_targets(
                selected_prometheus,
                verified_exporters,
                timeout_seconds=timeout_seconds(command.timeout),
            )

        helm.install_platform(
            release=platform,
            source_images=source_images,
            values=command.values,
            frontend_mode=command.frontend_mode,
            gateway_name=command.gateway_name,
            gateway_namespace=command.gateway_namespace,
            gateway_section_name=command.gateway_section_name,
            gateway_controller_name=gateway_plan.controller_name,
            observability_labels=observability_labels,
            observability_prometheus=f"{selected_prometheus.namespace}/{selected_prometheus.name}",
            gpu_resource_name=gpu_resource_name,
            rdma_resource_name=rdma.resource_name,
            rdma_managed=rdma.managed,
            rdma_node_names=rdma.node_names,
            stored_values=stored_values[0] if platform_exists else None,
            timeout=command.timeout,
        )
        if rdma.managed:
            live_discovery = ExporterDiscovery(kubectl, command.timeout)
            live_runtime = _select_runtime(live_discovery.nodes, runtime_scope)
            allocation = select_rdma(
                kubectl,
                live_runtime.nodes if live_runtime is not None else (),
                live_discovery.daemonsets,
                (),
                (platform.name, platform.namespace),
            )
            _print_plan(
                "RDMA",
                "Available" if allocation.available else "Not available",
                allocation.detail if allocation.available else (
                    "device plugin has no allocatable RDMA pool on the GPU nodes; "
                    "check node drivers and network interfaces"
                ),
            )
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
        if load_balancer_plan.install:
            _print_plan(
                "LoadBalancer",
                "Configured",
                f"{load_balancer_plan.release.display_name} (Layer 2; address allocation is confirmed per Service)",
            )
        if install_managed_prometheus:
            _print_plan("Prometheus", "Ready", managed_prometheus.display_name)
        if install_managed_dcgm:
            _print_plan("NVIDIA DCGM Exporter", "Ready", managed_dcgm.display_name)
        _print_plan("Foretoken platform", "Ready", platform.display_name)
        if not load_balancer_plan.install and load_balancer_plan.action != "Reuse":
            _print_plan(
                "LoadBalancer support",
                load_balancer_plan.action,
                "model services cannot receive external addresses until the "
                "cluster assigns LoadBalancer addresses; set "
                "loadBalancer.managedAddresses in --values to install managed MetalLB",
            )

    def uninstall(self, command: UninstallCommand) -> None:
        """Remove CLI-owned releases after user services are gone."""
        helm = self._helm
        kubectl = self._kubectl
        gateway = self._gateway
        load_balancer = self._load_balancer
        metax_exporter = self._metax_exporter
        timeout_seconds(command.timeout)
        platform = helm.platform_release()
        managed_dcgm = helm.dcgm_release()
        managed_prometheus = helm.prometheus_release()

        platform_exists = helm.release_exists(platform)
        if platform_exists and not helm.is_cleanup_managed(platform):
            raise DeploymentError(
                f"Helm release {platform.display_name} is not managed by foretoken; "
                "use its existing Helm lifecycle"
            )

        dcgm_exists = helm.release_exists(managed_dcgm)
        dcgm_managed = dcgm_exists and helm.is_cleanup_managed(managed_dcgm)
        prometheus_exists = helm.release_exists(managed_prometheus)
        prometheus_managed = (
            prometheus_exists and helm.is_cleanup_managed(managed_prometheus)
        )
        metax_managed = bool(metax_exporter.managed_resources())
        gateway_plan = gateway.resolve_uninstall(
            platform, platform_exists=platform_exists
        )
        if (
            platform_exists
            or dcgm_managed
            or prometheus_managed
            or metax_managed
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
            require_unused_managed_rdma(kubectl, (platform.name, platform.namespace))
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
        if metax_managed:
            _print_plan(
                "MetaX mxExporter",
                "Remove",
                f"{metax_exporter.namespace}/{metax_exporter.daemonset.name}",
            )
        else:
            _print_plan("MetaX mxExporter", "Preserve", "not CLI-managed")
        _print_plan(
            "Gateway Controller", gateway_plan.action, gateway_plan.detail
        )
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
        if metax_managed:
            metax_exporter.uninstall(command.timeout)
            _print_plan(
                "MetaX mxExporter",
                "Removed",
                f"{metax_exporter.namespace}/{metax_exporter.daemonset.name}",
            )
        gateway_result = gateway.finish_uninstall(
            gateway_plan, command.timeout
        )
        if gateway_result is not None:
            _print_plan("Gateway Controller", *gateway_result)
        _print_plan("LeaderWorkerSet", *self._leader_worker.finish_uninstall(command.timeout))
        load_balancer_result = load_balancer.finish_uninstall(command.timeout)
        if load_balancer_result is not None:
            _print_plan("LoadBalancer", *load_balancer_result)
