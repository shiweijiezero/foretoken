# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken CLI entry point."""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

from foretoken_cli.arguments import (
    BenchCommand,
    DeleteCommand,
    DeployCommand,
    EndpointCommand,
    InstallCommand,
    StatusCommand,
    UninstallCommand,
    parse_arguments,
)
from foretoken_cli.helm import (
    Helm,
    dcgm_release,
    platform_release,
    prometheus_release,
)
from foretoken_cli.kubernetes import (
    Kubectl,
    ResourceProgress,
    control_plane_deployments,
    load_deployment,
    mark_managed_metrics_scraper_namespace,
    namespace_progress,
    platform_service_resources,
    read_progress,
    resolve_frontend_endpoint,
    timeout_seconds,
    unmark_managed_metrics_scraper_namespace,
    wait_for_resources,
)
from foretoken_cli.accelerators._exporter import ExporterMonitor
from foretoken_cli.accelerators.metax import discover_metax_metrics
from foretoken_cli.accelerators.nvidia import discover_nvidia_metrics
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.observability import (
    PrometheusRef,
    prometheus_selects_service_monitor,
    select_prometheus,
)
from foretoken_cli.source import (
    prepare_source_images,
    restart_changed_source_deployments,
)


def _print_plan(responsibility: str, action: str, detail: str) -> None:
    """Print one stable installation lifecycle decision."""
    print(f"{responsibility:<28} {action:<8} {detail}")


def _require_prometheus_exporter_selection(
    kubectl: Kubectl,
    prometheus: PrometheusRef,
    exporter: ExporterMonitor,
    exporter_name: str,
) -> None:
    """Require a shared Prometheus to select one reused exporter monitor."""
    monitor = exporter.service_monitor
    if prometheus_selects_service_monitor(
        kubectl,
        prometheus,
        monitor.namespace,
        exporter.service_monitor_labels,
    ):
        return
    raise DeploymentError(
        f"Prometheus {prometheus.namespace}/{prometheus.name} does not select "
        f"{exporter_name} ServiceMonitor {monitor.namespace}/{monitor.name}; "
        "update the shared platform selectors before installing Foretoken"
    )


def _install(command: InstallCommand) -> None:
    """Install managed observability and update the Foretoken platform release."""
    timeout_seconds(command.timeout)
    helm = Helm()
    kubectl = Kubectl()

    platform = platform_release(command.namespace)
    platform_exists = helm.release_exists(platform)
    if platform_exists and not helm.is_cli_managed(platform):
        raise DeploymentError(
            f"Helm release {platform.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )
    if platform_exists:
        install_source = helm.release_install_source(platform) or "release"
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
    if not platform_exists:
        deployments = control_plane_deployments(kubectl)
        if deployments:
            existing = ", ".join(
                f"{deployment.namespace}/{deployment.display_name}"
                for deployment in deployments
            )
            raise DeploymentError(
                "a Foretoken control plane already exists; use its Helm lifecycle: "
                f"{existing}"
            )

    managed_dcgm = dcgm_release(command.namespace)
    managed_dcgm_exists = helm.release_exists(managed_dcgm)
    if managed_dcgm_exists and not helm.is_cli_managed(managed_dcgm):
        raise DeploymentError(
            f"Helm release {managed_dcgm.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )
    nvidia_metrics = discover_nvidia_metrics(
        kubectl, managed_dcgm if managed_dcgm_exists else None
    )
    metax_metrics = discover_metax_metrics(kubectl)

    managed_prometheus = prometheus_release(command.namespace)
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
            kubectl, command.namespace, command.prometheus
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
    if selected_prometheus is not None:
        if nvidia_metrics is not None and nvidia_metrics.reused_exporter is not None:
            _require_prometheus_exporter_selection(
                kubectl,
                selected_prometheus,
                nvidia_metrics.reused_exporter,
                "DCGM",
            )
        if metax_metrics is not None:
            _require_prometheus_exporter_selection(
                kubectl,
                selected_prometheus,
                metax_metrics,
                "mxExporter",
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
    elif nvidia_metrics.reused_exporter is not None:
        nvidia_action = "Reuse"
        nvidia_detail = (
            f"{nvidia_metrics.reused_exporter.daemonset.namespace}/"
            f"{nvidia_metrics.reused_exporter.daemonset.display_name}"
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
    monitor_namespaces = {command.namespace}
    if nvidia_metrics is not None and nvidia_metrics.reused_exporter is not None:
        monitor_namespaces.add(nvidia_metrics.reused_exporter.service_monitor.namespace)
    if metax_metrics is not None:
        monitor_namespaces.add(metax_metrics.service_monitor.namespace)

    platform_action = "Upgrade" if platform_exists else "Install"
    if command.editable is not None:
        _print_plan("Source images", "Build", command.editable)
    _print_plan("Prometheus", prometheus_action, prometheus_detail)
    _print_plan("NVIDIA DCGM Exporter", nvidia_action, nvidia_detail)
    _print_plan("MetaX mxExporter", metax_action, metax_detail)
    _print_plan("Foretoken platform", platform_action, platform.display_name)

    source_images = (
        prepare_source_images(
            command.editable,
            command.registry,
            command.namespace,
            command.timeout,
            command.dry_run,
        )
        if command.editable is not None
        else None
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
    dry_run_api_versions = (
        (
            "monitoring.coreos.com/v1/ServiceMonitor",
            "monitoring.coreos.com/v1/PrometheusRule",
        )
        if command.dry_run and install_managed_prometheus
        else ()
    )
    helm.install_platform(
        release=platform,
        source_images=source_images,
        values=command.values,
        frontend_mode=command.frontend_mode,
        gateway_name=command.gateway_name,
        gateway_namespace=command.gateway_namespace,
        gateway_section_name=command.gateway_section_name,
        observability_labels=observability_labels,
        reuse_values=platform_exists,
        timeout=command.timeout,
        dry_run=command.dry_run,
        dry_run_api_versions=dry_run_api_versions,
    )
    if not command.dry_run:
        if source_images is not None:
            restart_changed_source_deployments(
                kubectl,
                source_images,
                command.namespace,
                command.timeout,
            )
        if install_managed_prometheus:
            mark_managed_metrics_scraper_namespace(
                kubectl, managed_prometheus.namespace
            )
            _print_plan("Prometheus", "Ready", managed_prometheus.display_name)
        if install_managed_dcgm:
            _print_plan("NVIDIA DCGM Exporter", "Ready", managed_dcgm.display_name)
        _print_plan("Foretoken platform", "Ready", platform.display_name)


def _uninstall(command: UninstallCommand) -> None:
    """Remove CLI-owned releases after user services are gone."""
    timeout_seconds(command.timeout)
    helm = Helm()
    kubectl = Kubectl()
    platform = platform_release(command.namespace)
    managed_dcgm = dcgm_release(command.namespace)
    managed_prometheus = prometheus_release(command.namespace)

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
    if platform_exists or dcgm_managed or prometheus_managed:
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


def _deployment_resources(
    kustomize_path: str, kubectl: Kubectl
) -> tuple[ResourceRef, ...]:
    """Render a Kustomize root and return its user-facing service resources."""
    return load_deployment(kustomize_path, kubectl).service_refs()


def _report_progress(elapsed: float, progress: ResourceProgress) -> None:
    """Print one changed service state as a line suitable for terminals and logs."""
    detail = progress.detail
    suffix = f" — {detail}" if detail else ""
    print(
        f"[{elapsed:6.1f}s] {progress.resource.display_name:<48} "
        f"{progress.state}{suffix}",
        flush=True,
    )


def _print_status(progress: tuple[ResourceProgress, ...]) -> None:
    """Print a point-in-time readiness table for selected services."""
    resource_width = max(
        len("RESOURCE"), *(len(item.resource.display_name) for item in progress)
    )
    state_width = max(len("STATUS"), *(len(item.state) for item in progress))
    print(f"{'RESOURCE':<{resource_width}}  {'STATUS':<{state_width}}  DETAILS")
    for item in progress:
        print(
            f"{item.resource.display_name:<{resource_width}}  "
            f"{item.state:<{state_width}}  {item.detail}"
        )


def _deploy(kustomize_path: str, timeout: str) -> None:
    """Apply one Kustomize deployment and wait for current-generation readiness."""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    timeout_seconds(timeout)
    namespace = deployment.namespace or "<current>"
    print(f"Applying {deployment.path} to namespace {namespace}")
    kubectl.apply(deployment.rendered)
    print(f"Waiting up to {timeout} for Foretoken services")
    started = time.monotonic()
    wait_for_resources(
        deployment.service_refs(),
        kubectl,
        timeout,
        report=_report_progress,
    )
    print(f"Foretoken deployment is ready in {time.monotonic() - started:.1f}s")


def _delete(kustomize_path: str, timeout: str) -> None:
    """Delete one rendered deployment and wait for resource termination."""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    timeout_seconds(timeout)
    namespace = deployment.namespace or "<current>"
    print(f"Deleting {deployment.path} from namespace {namespace}")
    kubectl.delete(deployment.rendered, timeout)
    print("Foretoken deployment deleted")


def _status(
    kustomize_path: str | None, namespace: str | None, watch: bool
) -> None:
    """Inspect a rendered deployment or all services in one namespace."""
    kubectl = Kubectl()
    deployment_resources = (
        _deployment_resources(kustomize_path, kubectl)
        if kustomize_path is not None
        else None
    )

    def selected_progress() -> tuple[ResourceProgress, ...]:
        """Read fixed deployment targets or the namespace's current services."""
        if deployment_resources is not None:
            return read_progress(deployment_resources, kubectl)
        return namespace_progress(namespace or "", kubectl)

    if not watch:
        _print_status(selected_progress())
        return

    started = time.monotonic()
    previous: dict[ResourceRef, tuple[str, str, str]] = {}
    while True:
        progress = selected_progress()
        elapsed = time.monotonic() - started
        for item in progress:
            signature = (item.state, item.reason, item.message)
            if previous.get(item.resource) != signature:
                _report_progress(elapsed, item)
                previous[item.resource] = signature
        time.sleep(2)


def _endpoint(kustomize_path: str, timeout: str, host: bool) -> None:
    """Wait for and print the public endpoint of one rendered deployment."""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    print("Waiting for the frontend endpoint", file=sys.stderr, flush=True)
    endpoint = resolve_frontend_endpoint(deployment, kubectl, timeout)
    if host:
        print(endpoint.routing_host or urlsplit(endpoint.url).netloc)
        return
    print(endpoint.url)


def _bench(arguments: Sequence[str]) -> None:
    """Load optional benchmark dependencies only when the bench command runs."""
    try:
        from benchmarks.main import main as benchmark_main
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith(("benchmarks", "foretoken_cli")):
            raise SystemExit(
                "foretoken bench requires benchmark dependencies; "
                "install them with: pip install 'foretoken-cli[bench]'"
            ) from exc
        raise
    benchmark_main(arguments)


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch Foretoken deployment, status, and benchmark commands."""
    command = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        if isinstance(command, InstallCommand):
            _install(command)
        elif isinstance(command, UninstallCommand):
            _uninstall(command)
        elif isinstance(command, DeployCommand):
            _deploy(command.kustomize_path, command.timeout)
        elif isinstance(command, DeleteCommand):
            _delete(command.kustomize_path, command.timeout)
        elif isinstance(command, StatusCommand):
            _status(command.kustomize_path, command.namespace, command.watch)
        elif isinstance(command, EndpointCommand):
            _endpoint(
                command.kustomize_path,
                command.timeout,
                command.host,
            )
        elif isinstance(command, BenchCommand):
            _bench(command.arguments)
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
