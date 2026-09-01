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
from foretoken_cli.helm import Helm, platform_release, prometheus_release
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
from foretoken_cli.manifest import DeploymentError, ResourceRef
from foretoken_cli.observability import PrometheusRef, select_prometheus


def _print_plan(responsibility: str, action: str, detail: str) -> None:
    """Print one stable installation lifecycle decision."""
    print(f"{responsibility:<28} {action:<8} {detail}")


def _install(command: InstallCommand) -> None:
    """Install managed observability and update the Foretoken platform release."""
    helm = Helm()
    kubectl = Kubectl()
    platform = platform_release()
    platform_exists = helm.release_exists(platform)
    if platform_exists and not helm.is_cli_managed(platform):
        raise DeploymentError(
            f"Helm release {platform.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )

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

    managed_prometheus = prometheus_release()
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
    platform_action = "Upgrade" if platform_exists else "Install"
    _print_plan("Prometheus", prometheus_action, prometheus_detail)
    _print_plan("Foretoken platform", platform_action, platform.display_name)

    if install_managed_prometheus:
        helm.install_prometheus(
            managed_prometheus,
            command.timeout,
            command.dry_run,
        )
    observability_labels = (
        () if selected_prometheus is None else selected_prometheus.additional_labels
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
        if install_managed_prometheus:
            mark_managed_metrics_scraper_namespace(
                kubectl, managed_prometheus.namespace
            )
            _print_plan("Prometheus", "Ready", managed_prometheus.display_name)
        _print_plan("Foretoken platform", "Ready", platform.display_name)

def _uninstall(command: UninstallCommand) -> None:
    """Remove CLI-owned releases after user services are gone."""
    helm = Helm()
    kubectl = Kubectl()
    platform = platform_release()
    managed_prometheus = prometheus_release()

    platform_exists = helm.release_exists(platform)
    if platform_exists and not helm.is_cli_managed(platform):
        raise DeploymentError(
            f"Helm release {platform.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )

    prometheus_exists = helm.release_exists(managed_prometheus)
    prometheus_managed = (
        prometheus_exists and helm.is_cli_managed(managed_prometheus)
    )
    if platform_exists or prometheus_managed:
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
