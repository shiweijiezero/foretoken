# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Installation lifecycle for the Foretoken platform and managed dependencies."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
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
from foretoken.build_storage import (
    managed_build_storage_resources,
    remove_build_storage,
)
from foretoken.cluster_build import BuildCancellation
from foretoken.kubernetes import (
    Kubectl,
    control_plane_deployments,
    mark_managed_metrics_scraper_namespace,
    platform_service_resources,
    timeout_seconds,
    unmark_managed_metrics_scraper_namespace,
)
from foretoken.manifest import DeploymentError
from foretoken.namespace_mutation import NamespaceMutation
from foretoken.observability import PrometheusRef, select_prometheus
from foretoken.platform.config import (
    default_platform_config,
    grafana_anonymous_access_from_values,
    load_platform_values,
    resolve_load_balancer_config,
    runtime_overrides_from_values,
)
from foretoken.platform.gateway import GatewayControllerLifecycle
from foretoken.platform.helm import Helm
from foretoken.platform.leader_worker import LeaderWorkerLifecycle
from foretoken.platform.load_balancer import LoadBalancerLifecycle
from foretoken.platform.logs import LogCollectionLifecycle, log_config_from_values
from foretoken.platform.model_distribution import (
    ModelDistributionLifecycle,
    dragonfly_config_from_values,
)
from foretoken.platform.rdma import (
    migrate_stored_rdma_values,
    require_unused_managed_rdma,
    select_rdma,
)
from foretoken.platform.types import RuntimeOverrides
from foretoken.source import (
    forget_install,
    has_source_state,
    prepare_source_images,
    reconcile_source_resources,
    record_install,
    remove_build_caches,
    resolve_source_base_image,
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
    value = ((node.get("status") or {}).get("allocatable") or {}).get(resource_name)
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
                if ((node.get("metadata") or {}).get("labels") or {}).get(key) == value
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
            node
            for node in selected_nodes
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
            self._kubectl,
            config.management_label,
            config.metax_exporter_image,
            config.image_registry,
        )
        self._gateway = GatewayControllerLifecycle(self._helm, self._kubectl)
        self._leader_worker = LeaderWorkerLifecycle(self._helm, self._kubectl)
        self._load_balancer = LoadBalancerLifecycle(self._helm, self._kubectl)
        self._logs = LogCollectionLifecycle(self._helm)
        self._model_distribution = ModelDistributionLifecycle(self._helm, self._kubectl)

    def install(
        self,
        command: InstallCommand,
        *,
        source_base_image: str | None = None,
        source_build_arguments: dict[str, str] | None = None,
    ) -> None:
        """Install the platform and commit successful source inputs to its workstation binding."""
        with ExitStack() as artifacts:
            self._install(
                command,
                artifacts,
                source_base_image=source_base_image,
                source_build_arguments=source_build_arguments,
            )

    def _install(
        self,
        command: InstallCommand,
        artifacts: ExitStack,
        *,
        source_base_image: str | None,
        source_build_arguments: dict[str, str] | None,
    ) -> None:
        """Reconcile managed dependencies and the platform within the installation's artifact lifetime."""
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
        install_source = helm.release_install_source(platform) if platform_exists else None
        if install_source == "source" and command.editable is None:
            raise DeploymentError(
                f"Helm release {platform.display_name} uses source images; "
                "run foretoken install -e ."
            )
        values = load_platform_values(command.values)
        current_runtime = runtime_overrides_from_values(values)
        stored_values = (
            (migrate_stored_rdma_values(helm.release_user_values(platform)),)
            if platform_exists
            else ()
        )
        if stored_values:
            stored_logs = stored_values[0].get("observability", {}).get("logs", {})
            if "storageSize" in stored_logs:
                stored_logs.setdefault("initialSize", stored_logs.pop("storageSize"))
        log_config = log_config_from_values((*stored_values, *values))
        log_plan = self._logs.plan(log_config)
        dragonfly_plan = self._model_distribution.resolve_install(
            dragonfly_config_from_values((*stored_values, *values))
        )
        grafana_anonymous_access = grafana_anonymous_access_from_values(
            (*stored_values, *values)
        )
        if command.grafana_auth is not None:
            grafana_anonymous_access = command.grafana_auth == "anonymous"
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
        build_metax_runtime = False
        configured_runtime_image = (
            current_runtime.image
            if current_runtime.image is not None
            else stored_runtime.image
        )
        if command.editable is not None:
            source_runtime_image = resolve_source_base_image(
                kubectl,
                Path(command.editable).expanduser().resolve(),
                configured_runtime_image if install_source == "release" else current_runtime.image,
                source_base_image,
            )
            if (
                source_runtime_image is None
                and runtime_selection is not None
                and runtime_selection.backend == "metax"
            ):
                build_metax_runtime = True
            elif (
                source_runtime_image is None
                and runtime_selection is not None
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
            helm.dcgm_resource(managed_dcgm) if managed_dcgm_exists else None
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
        if not install_managed_prometheus and grafana_anonymous_access is not None:
            raise DeploymentError(
                "observability.grafana.anonymousAccess configures only Grafana installed "
                "by foretoken; configure the reused monitoring stack through its owner"
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
        _print_plan("Gateway Controller", gateway_plan.action, gateway_plan.detail)
        _print_plan("Prometheus", prometheus_action, prometheus_detail)
        for responsibility, action, detail in log_plan:
            _print_plan(responsibility, action, detail)
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
        _print_plan(
            "LeaderWorkerSet", leader_worker_plan.action, leader_worker_plan.detail
        )
        if dragonfly_plan.config.enabled:
            _print_plan(
                "Model file distribution",
                dragonfly_plan.action,
                dragonfly_plan.release.display_name,
            )
        _print_plan("Foretoken platform", platform_action, platform.display_name)

        load_balancer.apply(load_balancer_plan, command.timeout)
        gateway.apply_before_platform(gateway_plan, command.timeout)
        self._leader_worker.apply(leader_worker_plan, command.timeout)
        _print_plan("LeaderWorkerSet", "Ready", leader_worker_plan.detail)
        dragonfly_socket = self._model_distribution.apply(
            dragonfly_plan,
            runtime_selection.resource_name if runtime_selection is not None else "",
            command.timeout,
            node_names=tuple(
                node["metadata"]["name"] for node in runtime_selection.nodes
            )
            if runtime_selection is not None
            else (),
        )

        installation_workers = ThreadPoolExecutor(max_workers=2)
        source_cancellation = BuildCancellation()
        source_future = None
        dependencies: Future[None] = Future()
        # Only the worker enters source contexts. Join it before the outer stack
        # closes them, preserving installation errors through normal context exit.
        source_artifacts = artifacts.enter_context(ExitStack())
        try:
            if command.editable is not None:
                # Bootstrap shared storage and its namespace before Helm releases
                # can concurrently create resources in that namespace.
                origin = helm.prepare_source_origin(
                    Path(command.editable).expanduser().resolve(),
                    (*stored_values, *values),
                    command.timeout,
                )
                context = prepare_source_images(
                    command,
                    platform.namespace,
                    (*stored_values, *values),
                    source_runtime_image,
                    origin=origin,
                    build_metax_runtime=build_metax_runtime,
                    runtime_backend=runtime_selection.backend
                    if runtime_selection
                    else "nvidia",
                    saved_arguments=source_build_arguments,
                    before_delivery=dependencies.result,
                    cancellation=source_cancellation,
                )
                source_future = installation_workers.submit(
                    source_cancellation.run_stage, source_artifacts.enter_context, context
                )
            elif not kubectl.exists("namespace", platform.namespace):
                kubectl.run(["create", "namespace", platform.namespace])

            # A new Prometheus installation must establish its CRDs before restoring
            # custom log-chart monitoring resources. Existing monitoring can update in parallel.
            log_future = (
                installation_workers.submit(
                    self._logs.install, log_config, command.timeout
                )
                if managed_prometheus_exists or not install_managed_prometheus
                else None
            )
            if install_managed_prometheus:
                helm.install_prometheus(
                    managed_prometheus,
                    tuple(sorted(monitor_namespaces)),
                    managed_prometheus_exists,
                    command.timeout,
                    anonymous_access=grafana_anonymous_access,
                )
                mark_managed_metrics_scraper_namespace(
                    kubectl, managed_prometheus.namespace
                )
                resource = helm.prometheus_resource(managed_prometheus)
                selected_prometheus = PrometheusRef(
                    resource.name, resource.namespace, ()
                )
            if install_managed_dcgm:
                helm.install_dcgm_exporter(
                    managed_dcgm,
                    observability_labels,
                    nvidia_metrics.node_selector,
                    managed_dcgm_exists,
                    command.timeout,
                    visible_devices=nvidia_metrics.visible_devices,
                )
                # The chart permits one unavailable DaemonSet Pod during Helm's
                # wait, while discovery requires Ready coverage on every GPU node.
                # Complete the managed rollout before resolving it as existing.
                kubectl.rollout_status(
                    helm.dcgm_resource(managed_dcgm), command.timeout
                )
            if install_managed_metax:
                metax_exporter.install(
                    metax_metrics.node_names, observability_labels, command.timeout
                )

            if (
                install_managed_prometheus
                or install_managed_dcgm
                or install_managed_metax
            ):
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

            log_endpoint, log_storage_statefulset = (
                log_future.result()
                if log_future is not None
                else self._logs.install(log_config, command.timeout)
            )
            dependencies.set_result(None)
            source_images = (
                source_future.result() if source_future is not None else None
            )
        except BaseException as error:
            if not dependencies.done():
                dependencies.set_exception(error)
            if source_future is not None:
                source_future.cancel()
            source_cancellation.cancel(error)
            if isinstance(error, (DeploymentError, OSError)):
                source_cancellation.check()
            raise
        finally:
            # Resolve dependency success or failure before joining: the source
            # worker may be waiting to deliver node images immediately before Helm.
            installation_workers.shutdown(wait=True)

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
            grafana_anonymous_access=grafana_anonymous_access,
            log_endpoint=log_endpoint,
            log_storage_statefulset=log_storage_statefulset,
            gpu_resource_name=gpu_resource_name,
            rdma_resource_name=rdma.resource_name,
            rdma_managed=rdma.managed,
            rdma_node_names=rdma.node_names,
            dragonfly_socket_path=dragonfly_socket,
            stored_values=stored_values[0] if platform_exists else None,
            timeout=command.timeout,
        )
        if platform_exists:
            self._adopt_existing_deployments(command)
        if (
            not dragonfly_plan.config.enabled
            or dragonfly_plan.release != helm.dragonfly_release()
        ) and helm.release_exists(helm.dragonfly_release()):
            _print_plan(
                "Model file distribution",
                *self._model_distribution.finish_uninstall(command.timeout),
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
                allocation.detail
                if allocation.available
                else (
                    "device plugin has no allocatable RDMA pool on the GPU nodes; "
                    "check node drivers and network interfaces"
                ),
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
        if source_images is not None:
            record_install(
                kubectl,
                command,
                source_runtime_image,
                source_images.inputs,
                build_state=source_images.build_state,
            )
            reconcile_source_resources(kubectl, command.timeout)
        _print_plan("Foretoken platform", "Ready", platform.display_name)
        if not load_balancer_plan.install and load_balancer_plan.action != "Reuse":
            _print_plan(
                "LoadBalancer support",
                load_balancer_plan.action,
                "model services cannot receive external addresses until the "
                "cluster assigns LoadBalancer addresses; set "
                "loadBalancer.managedAddresses in --values to install managed MetalLB",
            )

    def _adopt_existing_deployments(self, command: InstallCommand) -> None:
        """Give legacy services a namespace owner without advancing their recorded runtime selections.

        Discovery also resumes an interrupted handoff: services remain authoritative
        after the shared controller has gone. Independent namespace controllers keep
        their own lifecycle, and no service intent or deployment revision is rewritten.
        """
        helm, kubectl = self._helm, self._kubectl
        namespaces = {resource.namespace for resource in platform_service_resources(kubectl)}
        defaults = None
        for namespace in sorted(namespaces):
            with NamespaceMutation(kubectl, namespace, command.timeout).hold():
                # Deletion or a candidate deployment may have completed since discovery.
                if not any(resource.namespace == namespace for resource in platform_service_resources(kubectl)):
                    continue
                installed = {release.namespace: release for release in helm.deployment_releases()}
                release = installed.get(namespace)
                if release is not None:
                    status = json.loads(helm.run([
                        "status", release.name, "--namespace", release.namespace, "--output", "json",
                    ]).stdout)["info"]["status"]
                    if status == "deployed":
                        continue
                    # Retry a failed release with its own recorded runtime choices.
                    values = helm.release_user_values(release)
                else:
                    if any(resource.namespace == namespace for resource in control_plane_deployments(kubectl)):
                        _print_plan("Serving controller", "Preserve", f"existing controller in {namespace}")
                        continue
                    if defaults is None:
                        defaults = helm.deployment_values(kubectl)
                    values = defaults
                    release = helm.deployment_release(namespace)
                _print_plan("Serving controller", "Adopt", release.display_name)
                helm.install_deployment(
                    release, values, command.timeout,
                    source_root=Path(command.editable).expanduser().resolve() if command.editable else None,
                )
            helm.wait_control_plane(release, command.timeout)

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

        source_storage = managed_build_storage_resources(kubectl, platform.namespace)
        source_builds = (
            platform_exists and helm.release_install_source(platform) == "source"
        ) or has_source_state(kubectl) or bool(source_storage)
        unadopted_origin = (
            () if platform_exists else helm.application_origin_resources()
        )
        admission_claims = {
            claim["uid"]: claim for claim in helm.admission_store_claims(kubectl)
        }
        dcgm_exists = helm.release_exists(managed_dcgm)
        dcgm_managed = dcgm_exists and helm.is_cleanup_managed(managed_dcgm)
        prometheus_exists = helm.release_exists(managed_prometheus)
        prometheus_managed = prometheus_exists and helm.is_cleanup_managed(
            managed_prometheus
        )
        metax_managed = bool(metax_exporter.managed_resources())
        managed_logs = self._logs.managed_releases()
        gateway_plan = gateway.resolve_uninstall(
            platform, platform_exists=platform_exists
        )
        deployments = helm.deployment_releases()
        dragonfly_managed = helm.is_cli_managed(helm.dragonfly_release())
        if (
            platform_exists
            or source_storage
            or unadopted_origin
            or admission_claims
            or dcgm_managed
            or prometheus_managed
            or metax_managed
            or managed_logs
            or dragonfly_managed
            or gateway_plan.managed
            or deployments
        ):
            resources = platform_service_resources(kubectl)
            if resources or deployments:
                remaining = ", ".join([
                    *(f"{resource.namespace}/{resource.display_name}" for resource in resources),
                    *(release.display_name for release in deployments),
                ])
                raise DeploymentError(
                    "delete Foretoken deployments and wait for their services and controllers to be removed before uninstalling the platform: "
                    f"{remaining}"
                )

        if platform_exists:
            require_unused_managed_rdma(kubectl, (platform.name, platform.namespace))
            admission_claims.update(
                (claim["uid"], claim)
                for claim in helm.prepare_admission_store_uninstall(
                    kubectl, command.timeout
                )
            )
            _print_plan("Foretoken platform", "Remove", platform.display_name)
        else:
            _print_plan("Foretoken platform", "Skip", "not installed")
            if unadopted_origin:
                _print_plan("Application file storage", "Remove", platform.display_name)
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
        _print_plan("Gateway Controller", gateway_plan.action, gateway_plan.detail)
        for release in managed_logs:
            _print_plan(
                "Logs", "Remove", release.display_name + " (retain stored logs)"
            )
        helm.stop_platform_tasks(kubectl, command.timeout)
        if source_builds:
            reconcile_source_resources(kubectl, command.timeout, uninstall=True)
        self._logs.uninstall(managed_logs, command.timeout)
        if platform_exists:
            helm.uninstall(platform, command.timeout)
            _print_plan("Foretoken platform", "Removed", platform.display_name)
        elif unadopted_origin:
            helm.remove_application_origin(unadopted_origin, command.timeout)
            _print_plan("Application file storage", "Removed", platform.display_name)
        for claim in admission_claims.values():
            kubectl.wait_deleted("persistentvolumeclaim", claim, command.timeout)
        if admission_claims:
            _print_plan("Admission storage", "Removed", platform.display_name)
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
        gateway_result = gateway.finish_uninstall(gateway_plan, command.timeout)
        if gateway_result is not None:
            _print_plan("Gateway Controller", *gateway_result)
        _print_plan(
            "LeaderWorkerSet", *self._leader_worker.finish_uninstall(command.timeout)
        )
        if helm.release_exists(helm.dragonfly_release()):
            _print_plan(
                "Model file distribution",
                *self._model_distribution.finish_uninstall(command.timeout),
            )
        load_balancer_result = load_balancer.finish_uninstall(command.timeout)
        if load_balancer_result is not None:
            _print_plan("LoadBalancer", *load_balancer_result)
        if source_builds:
            remove_build_caches(kubectl, command.timeout)
        if source_storage:
            _print_plan(
                "Source build storage",
                *remove_build_storage(kubectl, platform.namespace, command.timeout),
            )
        forget_install(kubectl)
