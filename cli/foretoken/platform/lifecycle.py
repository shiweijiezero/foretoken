# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Coordinate platform installation and retirement across independently owned dependencies."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foretoken.accelerators.config import GPU_RESOURCE_BACKENDS
from foretoken.accelerators.discovery import ExporterDiscovery
from foretoken.arguments import InstallCommand, UninstallCommand
from foretoken.build.cluster.storage import (
    managed_build_storage_resources,
    remove_build_storage,
)
from foretoken.build.source import prepare_source_images
from foretoken.build.state import (
    forget_install,
    reconcile_source_resources,
    record_install,
    resolve_source_base_image,
    source_operation,
    source_uninstall,
)
from foretoken.deployment.instances import DEPLOYMENT_RESOURCE, DeploymentInstance
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import (
    Kubectl,
    ResourceRef,
    resource_ref,
    timeout_seconds,
)
from foretoken.kubernetes.mutation import DeploymentMutation
from foretoken.platform.config import (
    default_platform_config,
    grafana_anonymous_access_from_values,
    load_platform_values,
    migrate_stored_platform_values,
    resolve_load_balancer_config,
    runtime_overrides_from_values,
)
from foretoken.platform.helm import Helm
from foretoken.platform.helm_client import owned_by_release
from foretoken.platform.leader_worker import LeaderWorkerLifecycle, LeaderWorkerPlan
from foretoken.platform.networking.gateway import (
    GatewayControllerLifecycle,
    GatewayControllerPlan,
)
from foretoken.platform.networking.load_balancer import (
    LoadBalancerLifecycle,
    LoadBalancerPlan,
)
from foretoken.platform.observability.logs import (
    LogCollectionLifecycle,
    LogConfig,
    log_config_from_values,
)
from foretoken.platform.observability.monitoring import (
    MonitoringLifecycle,
    MonitoringPlan,
)
from foretoken.platform.prometheus import PrometheusRef
from foretoken.platform.rdma import (
    RDMASelection,
    require_unused_managed_rdma,
    select_rdma,
)
from foretoken.platform.storage.admission import AdmissionStoreRetirement
from foretoken.platform.storage.application_files import PlatformApplicationFiles
from foretoken.platform.storage.directories import remove_directory_bindings
from foretoken.platform.storage.model_distribution import (
    DragonflyPlan,
    ModelDistributionLifecycle,
)
from foretoken.platform.types import PlatformGatewayConfig, ReleaseRef, RuntimeOverrides
from foretoken.processes.execution import CommandExecution

if TYPE_CHECKING:
    from foretoken.build.source import SourceImages


@dataclass(frozen=True)
class _RuntimeSelection:
    """One accelerator backend and Kubernetes resource selected for the platform."""

    backend: str
    resource_name: str
    nodes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _RuntimePlan:
    """Resolve runtime intent once for dependency placement, source builds, and final chart values."""

    selection: _RuntimeSelection | None
    scope: RuntimeOverrides
    source_base_image: str | None
    build_metax: bool
    gpu_resource_override: str | None
    decision: tuple[str, str]


@dataclass(frozen=True)
class _InstallPlan:
    """Resolved component intent shared by installation and finalization stages."""

    platform: ReleaseRef
    platform_exists: bool
    values: tuple[dict[str, Any], ...]
    stored_values: dict[str, Any] | None
    runtime: _RuntimePlan
    rdma: RDMASelection
    load_balancer: LoadBalancerPlan
    gateway_config: PlatformGatewayConfig
    gateway: GatewayControllerPlan
    leader_worker: LeaderWorkerPlan
    log_config: LogConfig
    log_decisions: tuple[tuple[str, str, str], ...]
    model_distribution: DragonflyPlan
    monitoring: MonitoringPlan


@dataclass(frozen=True)
class _PreparedInstallation:
    """Ready dependency outputs whose source artifacts remain borrowed from the command's ExitStack."""

    source_images: SourceImages | None
    prometheus: PrometheusRef
    log_endpoint: str
    log_storage_statefulset: str


class PlatformLifecycle:
    """Own command scopes, source locks, and ordering between platform component lifecycles."""

    def __init__(self, oci_registry: str | None = None) -> None:
        config = default_platform_config(oci_registry)
        self._kubectl = Kubectl()
        self._helm = Helm(config, self._kubectl)
        self._gateway = GatewayControllerLifecycle(self._helm, self._kubectl)
        self._leader_worker = LeaderWorkerLifecycle(self._helm, self._kubectl)
        self._load_balancer = LoadBalancerLifecycle(self._helm, self._kubectl)
        self._logs = LogCollectionLifecycle(self._helm)
        self._monitoring = MonitoringLifecycle(self._helm, self._kubectl)
        self._model_distribution = ModelDistributionLifecycle(self._helm, self._kubectl)
        self._application_files = PlatformApplicationFiles(self._helm, self._kubectl)
        self._admission_store = AdmissionStoreRetirement(self._kubectl, self._helm.platform_release())

    def install(
        self, command: InstallCommand, *, source_base_image: str | None = None,
        source_build_arguments: dict[str, str] | None = None,
    ) -> None:
        """Install the platform and commit successful source inputs within one command's artifact lifetime."""
        with (
            (CommandExecution.current() or CommandExecution(timeout_seconds(command.timeout))) as execution,
            source_operation(self._kubectl, command.timeout, required=command.editable is not None),
            ExitStack() as artifacts,
        ):
            self._install(command, artifacts, execution, source_base_image, source_build_arguments)

    def uninstall(self, command: UninstallCommand) -> None:
        """Retire deployment instances and owned dependencies under exclusive source cleanup ownership."""
        with (
            CommandExecution.current() or CommandExecution(timeout_seconds(command.timeout)),
            source_operation(self._kubectl, command.timeout, prepare=False),
        ):
            self._uninstall(command)

    def _install(
        self, command: InstallCommand, artifacts: ExitStack, execution: CommandExecution,
        source_base_image: str | None, source_build_arguments: dict[str, str] | None,
    ) -> None:
        """Apply a resolved installation while retaining shared source publication and cleanup order."""
        plan = self._resolve_install(command, source_base_image)
        self._report_install(command, plan)
        self._load_balancer.apply(plan.load_balancer, command.timeout)
        self._gateway.apply_before_platform(plan.gateway, command.timeout)
        self._leader_worker.apply(plan.leader_worker, command.timeout)
        _print_plan("LeaderWorkerSet", "Ready", plan.leader_worker.detail)
        download_proxy = self._model_distribution.apply(
            plan.model_distribution,
            plan.runtime.selection.resource_name if plan.runtime.selection is not None else "",
            command.timeout,
        )
        prepared = self._prepare_dependencies_and_source(
            command, plan, artifacts, execution, download_proxy, source_build_arguments,
        )
        self._helm.install_platform(
            release=plan.platform,
            source_images=prepared.source_images,
            values=command.values,
            frontend_mode=command.frontend_mode,
            gateway_name=command.gateway_name,
            gateway_namespace=command.gateway_namespace,
            gateway_section_name=command.gateway_section_name,
            gateway_controller_name=plan.gateway.controller_name,
            observability_labels=plan.monitoring.labels,
            observability_prometheus=f"{prepared.prometheus.namespace}/{prepared.prometheus.name}",
            grafana_anonymous_access=plan.monitoring.anonymous_access,
            log_endpoint=prepared.log_endpoint,
            log_storage_statefulset=prepared.log_storage_statefulset,
            gpu_resource_name=plan.runtime.gpu_resource_override,
            rdma_resource_name=plan.rdma.resource_name,
            rdma_managed=plan.rdma.managed,
            rdma_node_names=plan.rdma.node_names,
            download_proxy_url=download_proxy,
            stored_values=plan.stored_values,
            kubectl=self._kubectl,
            application_files=self._application_files,
            timeout=command.timeout,
        )
        self._finish_install(command, plan, prepared, download_proxy)

    def _uninstall(self, command: UninstallCommand) -> None:
        """Fence new instances before draining existing ones and removing their platform dependencies."""
        helm, kubectl = self._helm, self._kubectl
        timeout_seconds(command.timeout)
        platform = helm.platform_release()
        platform_exists = helm.release_exists(platform)
        if platform_exists and not helm.is_cleanup_managed(platform):
            raise DeploymentError(
                f"Helm release {platform.display_name} is not managed by foretoken; use its existing Helm lifecycle"
            )
        configuration = kubectl.get_if_exists("configmap", platform.name + "-platform", platform.namespace)
        if configuration is not None and not owned_by_release(configuration, platform):
            raise DeploymentError(f"Platform configuration is not owned by {platform.display_name}")
        if configuration is not None:
            configuration = helm.set_platform_retiring(kubectl, configuration, retiring=True)
        platform_uid = configuration["metadata"]["uid"] if configuration is not None else ""
        source_storage = managed_build_storage_resources(kubectl, platform.namespace)
        application_origin = self._application_files.resources()
        origin_claims = tuple(
            document["metadata"] for document in application_origin
            if document["kind"] == "PersistentVolumeClaim"
        )
        unadopted_origin = () if platform_exists else application_origin
        admission_claims = {claim["uid"]: claim for claim in self._admission_store.claims()}
        monitoring = self._monitoring.resolve_uninstall()
        managed_logs = self._logs.managed_releases()
        gateway = self._gateway.resolve_uninstall(platform, platform_exists=platform_exists)
        deployments = self._retire_deployments(command.timeout)
        dragonfly_managed = helm.is_cli_managed(self._model_distribution.release)
        if (
            platform_exists or source_storage or unadopted_origin or admission_claims
            or monitoring.dcgm_managed or monitoring.prometheus_managed or monitoring.metax_managed
            or managed_logs or dragonfly_managed or gateway.managed or deployments
        ):
            resources = _platform_service_resources(kubectl)
            controllers = tuple(
                controller for controller in kubectl.list_all_resources(
                    ("deployment.apps",), label_selector="app.kubernetes.io/name=foretoken-control-plane",
                )
                if not owned_by_release(controller, platform)
            )
            if resources or controllers:
                remaining = ", ".join([
                    *(f"{resource.namespace}/{resource.display_name}" for resource in resources),
                    *(f"{value['metadata']['namespace']}/Deployment/{value['metadata']['name']}" for value in controllers),
                ])
                raise DeploymentError(
                    "Foretoken resources remain; finish their existing owner's cleanup "
                    f"before uninstalling the platform: {remaining}"
                )
        if platform_exists:
            require_unused_managed_rdma(kubectl, (platform.name, platform.namespace))
            admission_claims.update((claim["uid"], claim) for claim in self._admission_store.prepare(command.timeout))
            _print_plan("Foretoken platform", "Remove", platform.display_name)
        else:
            _print_plan("Foretoken platform", "Skip", "not installed")
            if unadopted_origin:
                _print_plan("Application file storage", "Remove", platform.display_name)
        for name, exists, managed, release in (
            ("NVIDIA DCGM Exporter", monitoring.dcgm_exists, monitoring.dcgm_managed, self._monitoring.dcgm),
            ("Prometheus", monitoring.prometheus_exists, monitoring.prometheus_managed, self._monitoring.prometheus),
        ):
            _print_plan(name, "Remove" if managed else "Preserve" if exists else "Skip", release.display_name if exists else "no managed release")
        metax = self._monitoring.metax_exporter
        _print_plan(
            "MetaX mxExporter", "Remove" if monitoring.metax_managed else "Preserve",
            f"{metax.namespace}/{metax.daemonset.name}" if monitoring.metax_managed else "not CLI-managed",
        )
        _print_plan("Gateway Controller", gateway.action, gateway.detail)
        for release in managed_logs:
            _print_plan("Logs", "Remove", release.display_name + " (retain stored logs)")

        # Instance and storage consumers must be gone before platform tasks, stores,
        # and Helm releases lose the owners that complete their cleanup.
        if remove_directory_bindings(kubectl, platform_uid, command.timeout):
            _print_plan("Directory bindings", "Removed", "retained directory files")
        helm.stop_platform_tasks(kubectl, command.timeout)
        with source_uninstall(
            kubectl, command.timeout, namespace=platform.namespace,
            origins=origin_claims, storage=source_storage,
        ):
            self._logs.uninstall(managed_logs, command.timeout)
            if platform_exists:
                helm.uninstall(platform, command.timeout)
                _print_plan("Foretoken platform", "Removed", platform.display_name)
            elif unadopted_origin:
                self._application_files.retire(unadopted_origin, command.timeout)
                _print_plan("Application file storage", "Removed", platform.display_name)
        for claim in admission_claims.values():
            kubectl.wait_deleted("persistentvolumeclaim", claim, command.timeout)
        if admission_claims:
            _print_plan("Admission storage", "Removed", platform.display_name)
        for decision in self._monitoring.uninstall(monitoring, command.timeout):
            _print_plan(*decision)
        result = self._gateway.finish_uninstall(gateway, command.timeout)
        if result is not None:
            _print_plan("Gateway Controller", *result)
        _print_plan("LeaderWorkerSet", *self._leader_worker.finish_uninstall(command.timeout))
        _print_plan("Model file distribution", *self._model_distribution.finish_uninstall(command.timeout))
        result = self._load_balancer.finish_uninstall(command.timeout)
        if result is not None:
            _print_plan("LoadBalancer", *result)
        if source_storage:
            _print_plan("Source build storage", *remove_build_storage(kubectl, platform.namespace, command.timeout))
        forget_install(kubectl)

    def _resolve_install(self, command: InstallCommand, source_base_image: str | None) -> _InstallPlan:
        """Resolve release ownership, explicit values, and component plans before dependency writes."""
        helm = self._helm
        timeout_seconds(command.timeout)
        platform = helm.platform_release()
        platform_exists = helm.release_exists(platform)
        if platform_exists and not helm.is_cli_managed(platform):
            raise DeploymentError(
                f"Helm release {platform.display_name} is not managed by foretoken; use its existing Helm lifecycle"
            )
        install_source = helm.release_install_source(platform) if platform_exists else None
        if install_source == "source" and command.editable is None:
            raise DeploymentError(f"Helm release {platform.display_name} uses source images; run foretoken install -e .")
        values = load_platform_values(command.values)
        stored = (migrate_stored_platform_values(helm.release_user_values(platform)),) if platform_exists else ()
        if stored:
            stored[0].pop("scope", None)
            stored[0].pop("namespaceAccess", None)
            logs = stored[0].get("observability", {}).get("logs", {})
            if "storageSize" in logs:
                logs.setdefault("initialSize", logs.pop("storageSize"))
        merged = (*stored, *values)
        log_config = log_config_from_values(merged)
        log_decisions = self._logs.plan(log_config)
        distribution = self._model_distribution.resolve_install(merged)
        anonymous_access = grafana_anonymous_access_from_values(merged)
        if command.grafana_auth is not None:
            anonymous_access = command.grafana_auth == "anonymous"
        load_balancer = self._load_balancer.resolve_install(resolve_load_balancer_config(values))
        if load_balancer.blocking:
            _print_plan("LoadBalancer", load_balancer.action, load_balancer.detail)
            raise DeploymentError(load_balancer.detail)
        gateway_config, gateway = self._gateway.resolve_install(command, platform, platform_exists)
        leader_worker = self._leader_worker.resolve_install(merged)
        discovery = ExporterDiscovery(self._kubectl, command.timeout)
        runtime = self._resolve_runtime(command, install_source, values, stored, discovery.nodes, source_base_image)
        rdma = select_rdma(
            self._kubectl, runtime.selection.nodes if runtime.selection is not None else (),
            discovery.daemonsets, merged, (platform.name, platform.namespace),
        )
        stored_runtime = runtime_overrides_from_values(stored)
        if platform_exists and (
            not rdma.managed or runtime.scope.gpu_node_selector != stored_runtime.gpu_node_selector
            or bool(set(stored[0].get("rdma", {}).get("nodeNames", [])) - set(rdma.node_names))
        ):
            require_unused_managed_rdma(self._kubectl, (platform.name, platform.namespace))
        monitoring = self._monitoring.resolve_install(discovery, command.prometheus, anonymous_access)
        return _InstallPlan(
            platform=platform,
            platform_exists=platform_exists,
            values=values,
            stored_values=stored[0] if stored else None,
            runtime=runtime,
            rdma=rdma,
            load_balancer=load_balancer,
            gateway_config=gateway_config,
            gateway=gateway,
            leader_worker=leader_worker,
            log_config=log_config,
            log_decisions=log_decisions,
            model_distribution=distribution,
            monitoring=monitoring,
        )

    def _resolve_runtime(
        self, command: InstallCommand, install_source: str | None,
        values: tuple[dict[str, Any], ...], stored: tuple[dict[str, Any], ...],
        nodes: tuple[dict[str, Any], ...], source_base_image: str | None,
    ) -> _RuntimePlan:
        """Resolve accelerator scope and source ABI choices without overriding installed user intent."""
        current = runtime_overrides_from_values(values)
        previous = runtime_overrides_from_values(stored)
        scope = RuntimeOverrides(
            gpu_resource_name=(
                current.gpu_resource_name
                if current.gpu_resource_name is not None
                else (
                    None if current.gpu_node_selector is not None
                    else previous.gpu_resource_name
                )
            ),
            gpu_node_selector=(
                current.gpu_node_selector
                if current.gpu_node_selector is not None
                else previous.gpu_node_selector
            ),
        )
        selection = _select_runtime(nodes, scope)
        configured_image = current.image if current.image is not None else previous.image
        source_image = None
        build_metax = False
        if command.editable is not None:
            source_image = resolve_source_base_image(
                self._kubectl, Path(command.editable).expanduser().resolve(),
                configured_image if install_source == "release" else current.image, source_base_image,
            )
            if source_image is None and selection is not None and selection.backend == "metax":
                build_metax = True
            elif source_image is None and selection is not None and selection.backend == "custom":
                raise DeploymentError(
                    "runtime.vllm.image must be set in --values for source builds on "
                    f"GPU resource {selection.resource_name}"
                )
        elif selection is not None and selection.backend == "custom" and configured_image in {None, "auto"}:
            raise DeploymentError(f"runtime.vllm.image must be set in --values for GPU resource {selection.resource_name}")
        resource_name = (
            selection.resource_name if selection is not None and current.gpu_resource_name is None
            and (previous.gpu_resource_name is None or current.gpu_node_selector is not None) else None
        )
        if selection is None:
            decision = "Default", "chart runtime (no allocatable supported GPU detected)"
        else:
            configured = any(value is not None for value in (
                current.gpu_resource_name, current.gpu_node_selector,
                previous.gpu_resource_name, previous.gpu_node_selector,
            ))
            decision = "Configured" if configured else "Auto-select", f"{selection.backend} via {selection.resource_name}"
        return _RuntimePlan(selection, scope, source_image, build_metax, resource_name, decision)

    def _prepare_dependencies_and_source(
        self, command: InstallCommand, plan: _InstallPlan, artifacts: ExitStack,
        execution: CommandExecution, download_proxy: str, source_build_arguments: dict[str, str] | None,
    ) -> _PreparedInstallation:
        """Join native dependency and source stages before their shared source artifacts can close."""
        workers = ThreadPoolExecutor(max_workers=2)
        source_future = None
        dependencies: Future[None] = Future()
        source_artifacts = artifacts.enter_context(ExitStack())
        try:
            if command.editable is not None:
                # Establish shared storage before Helm releases concurrently create
                # objects in its namespace. Only the source worker enters its contexts.
                merged = (*((plan.stored_values,) if plan.stored_values is not None else ()), *plan.values)
                rendered = self._helm.render_source_origin(
                    Path(command.editable).expanduser().resolve(), command.values, command.timeout,
                    stored_values=plan.stored_values,
                    download_proxy_url=download_proxy,
                )
                origin = self._application_files.prepare(rendered, command.timeout)
                assert origin is not None
                context = prepare_source_images(
                    command, plan.platform.namespace, merged, plan.runtime.source_base_image,
                    origin=origin, build_metax_runtime=plan.runtime.build_metax,
                    runtime_backend=plan.runtime.selection.backend if plan.runtime.selection else "nvidia",
                    saved_arguments=source_build_arguments, before_delivery=dependencies.result, cancellation=execution,
                )
                source_future = workers.submit(execution.run_stage, source_artifacts.enter_context, context)
            elif not self._kubectl.exists("namespace", plan.platform.namespace):
                self._kubectl.run(["create", "namespace", plan.platform.namespace])
            # Fresh monitoring must establish CRDs before log charts restore their
            # monitoring resources; existing monitoring permits parallel updates.
            log_future = (
                workers.submit(execution.run_stage, self._logs.install, plan.log_config, command.timeout)
                if plan.monitoring.prometheus_exists or plan.monitoring.reused_prometheus is not None else None
            )
            prometheus = self._monitoring.apply(plan.monitoring, command.timeout)
            log_endpoint, log_statefulset = (
                log_future.result() if log_future is not None else self._logs.install(plan.log_config, command.timeout)
            )
            dependencies.set_result(None)
            images = source_future.result() if source_future is not None else None
            return _PreparedInstallation(images, prometheus, log_endpoint, log_statefulset)
        except BaseException as error:
            if not dependencies.done():
                dependencies.set_exception(error)
            if source_future is not None:
                source_future.cancel()
            execution.cancel(error)
            if isinstance(error, (DeploymentError, OSError)):
                execution.check()
            raise
        finally:
            # The source worker may be waiting on dependencies before image delivery.
            # Resolve that future first, join workers, then let the caller close artifacts.
            workers.shutdown(wait=True)

    def _finish_install(
        self, command: InstallCommand, plan: _InstallPlan,
        prepared: _PreparedInstallation, download_proxy: str,
    ) -> None:
        """Finalize native dependencies and commit source provenance only after platform installation."""
        _print_plan("Model file distribution", *self._model_distribution.finish_uninstall(
            command.timeout, keep_release=plan.model_distribution.release if download_proxy else None,
            keep_proxy_url=download_proxy,
        ))
        if plan.rdma.managed:
            discovery = ExporterDiscovery(self._kubectl, command.timeout)
            runtime = _select_runtime(discovery.nodes, plan.runtime.scope)
            allocation = select_rdma(
                self._kubectl, runtime.nodes if runtime is not None else (), discovery.daemonsets,
                (), (plan.platform.name, plan.platform.namespace),
            )
            _print_plan(
                "RDMA", "Available" if allocation.available else "Not available",
                allocation.detail if allocation.available else (
                    "device plugin has no allocatable RDMA pool on the GPU nodes; check node drivers and network interfaces"
                ),
            )
        for decision in self._gateway.finish_update(plan.gateway, plan.gateway_config, command.timeout):
            _print_plan(*decision)
        if plan.load_balancer.install:
            _print_plan(
                "LoadBalancer", "Configured",
                f"{plan.load_balancer.release.display_name} (Layer 2; address allocation is confirmed per Service)",
            )
        if plan.monitoring.reused_prometheus is None:
            _print_plan("Prometheus", "Ready", self._monitoring.prometheus.display_name)
        if plan.monitoring.managed_nvidia is not None:
            _print_plan("NVIDIA DCGM Exporter", "Ready", self._monitoring.dcgm.display_name)
        if prepared.source_images is not None:
            record_install(
                self._kubectl, command, plan.runtime.source_base_image, prepared.source_images.inputs,
                build_state=prepared.source_images.build_state,
            )
            reconcile_source_resources(self._kubectl, command.timeout)
        _print_plan("Foretoken platform", "Ready", plan.platform.display_name)
        if not plan.load_balancer.install and plan.load_balancer.action != "Reuse":
            _print_plan(
                "LoadBalancer support", plan.load_balancer.action,
                "model services cannot receive external addresses until the cluster assigns LoadBalancer addresses; "
                "set loadBalancer.managedAddresses in --values to install managed MetalLB",
            )

    def _retire_deployments(self, timeout: str) -> tuple[DeploymentInstance, ...]:
        """Supersede each exact instance under its lock, then wait outside locks for native retirement."""
        kubectl = self._kubectl
        deployments = (
            DeploymentInstance.list(kubectl, all_namespaces=True)
            if DEPLOYMENT_RESOURCE in kubectl.api_resource_names("inference.foretoken.io") else ()
        )
        for deployment in deployments:
            _print_plan("Deployment instance", "Remove", f"{deployment.namespace}/{deployment.name}")
            mutation = DeploymentMutation(kubectl, deployment.namespace, deployment.name, timeout, uid=deployment.uid)
            try:
                with mutation.hold():
                    mutation.supersede()
                    deployment.delete(timeout, wait=False)
            except DeploymentError:
                current = kubectl.get_if_exists(DEPLOYMENT_RESOURCE, deployment.name, deployment.namespace)
                if current is not None and current["metadata"]["uid"] == deployment.uid and not current["metadata"].get("deletionTimestamp"):
                    raise
        for deployment in deployments:
            deployment.wait_deleted(timeout)
        return deployments

    def _report_install(self, command: InstallCommand, plan: _InstallPlan) -> None:
        """Describe resolved ownership decisions before applying the first dependency change."""
        if command.editable is not None:
            _print_plan("Source images", "Build", command.editable)
        _print_plan("LoadBalancer", plan.load_balancer.action, plan.load_balancer.detail)
        _print_plan("Gateway Controller", plan.gateway.action, plan.gateway.detail)
        _print_plan("Prometheus", *plan.monitoring.prometheus_decision)
        for decision in plan.log_decisions:
            _print_plan(*decision)
        for decision in plan.monitoring.exporter_decisions:
            _print_plan(*decision)
        _print_plan("Inference runtime", *plan.runtime.decision)
        _print_plan("RDMA", plan.rdma.action, plan.rdma.detail)
        _print_plan("LeaderWorkerSet", plan.leader_worker.action, plan.leader_worker.detail)
        _print_plan("Model file distribution", plan.model_distribution.action, plan.model_distribution.detail)
        _print_plan("Foretoken platform", "Upgrade" if plan.platform_exists else "Install", plan.platform.display_name)


def _print_plan(responsibility: str, action: str, detail: str) -> None:
    """Print one stable platform lifecycle decision."""
    print(f"{responsibility:<28} {action:<20} {detail}")


def _select_runtime(nodes: tuple[dict[str, Any], ...], overrides: RuntimeOverrides) -> _RuntimeSelection | None:
    """Select one supported accelerator resource within the administrator's configured scope."""
    selected_nodes = nodes
    if overrides.gpu_node_selector is not None:
        key, value = overrides.gpu_node_selector
        if key:
            selected_nodes = tuple(
                node for node in nodes
                if ((node.get("metadata") or {}).get("labels") or {}).get(key) == value
            )
    if overrides.gpu_resource_name is not None:
        resource_name = overrides.gpu_resource_name
        if not resource_name:
            return None
    else:
        resources = tuple(
            resource for resource in GPU_RESOURCE_BACKENDS
            if any(_resource_capacity(node, resource) > 0 for node in selected_nodes)
        )
        if not resources:
            return None
        if len(resources) > 1:
            raise DeploymentError(
                "multiple accelerator resources are allocatable in the selected cluster scope: "
                + ", ".join(resources)
                + "; set runtime.vllm.gpu.resourceName or runtime.vllm.gpu.nodeSelector in --values"
            )
        resource_name = resources[0]
    return _RuntimeSelection(
        GPU_RESOURCE_BACKENDS.get(resource_name, "custom"), resource_name,
        tuple(node for node in selected_nodes if _resource_capacity(node, resource_name) > 0),
    )


def _resource_capacity(node: dict[str, Any], resource_name: str) -> int:
    """Read one node's allocatable extended-resource capacity."""
    from kubernetes.utils.quantity import parse_quantity

    value = ((node.get("status") or {}).get("allocatable") or {}).get(resource_name)
    try:
        return int(parse_quantity(str(value)))
    except (TypeError, ValueError):
        return 0


def _platform_service_resources(kubectl: Kubectl) -> tuple[ResourceRef, ...]:
    """Find remaining reconciliation intent and descendants before their platform owner retires."""
    supported = set(kubectl.api_resource_names())
    kinds = tuple(sorted(name for name in supported if name.endswith(".inference.foretoken.io")))
    objects = list(kubectl.list_all_resources(kinds)) if kinds else []
    workloads = ("pods", "jobs", "replicasets", "deployments", "statefulsets")
    if "leaderworkersets.leaderworkerset.x-k8s.io" in supported:
        workloads += ("leaderworkersets.leaderworkerset.x-k8s.io",)
    for label in (
        "inference.foretoken.io/frontend-service", "inference.foretoken.io/model-group",
        "inference.foretoken.io/model-preparation-group",
    ):
        objects.extend(kubectl.list_all_resources(workloads, label_selector=label))
    return tuple(dict.fromkeys(resource_ref(value) for value in objects))
