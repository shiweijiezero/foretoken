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
from foretoken_cli.helm import Helm, platform_release
from foretoken_cli.kubernetes import (
    Kubectl,
    ResourceProgress,
    control_plane_deployments,
    load_deployment,
    namespace_progress,
    platform_service_resources,
    read_progress,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken_cli.manifest import DeploymentError, ResourceRef


def _print_plan(action: str, detail: str) -> None:
    """Print one stable platform lifecycle decision."""
    print(f"{'Foretoken platform':<28} {action:<8} {detail}")


def _install(command: InstallCommand) -> None:
    """Install or update the CLI-owned Foretoken platform release."""
    helm = Helm()
    release = platform_release()
    release_exists = helm.release_exists(release)
    if release_exists and not helm.is_cli_managed(release):
        raise DeploymentError(
            f"Helm release {release.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )
    deployments = control_plane_deployments(Kubectl())
    expected_deployment = f"{release.name}-control-plane"
    unexpected_deployments = tuple(
        deployment
        for deployment in deployments
        if not (
            release_exists
            and deployment.namespace == release.namespace
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

    action = "Upgrade" if release_exists else "Install"
    _print_plan(action, release.display_name)
    helm.install_platform(
        release=release,
        values=command.values,
        frontend_mode=command.frontend_mode,
        gateway_name=command.gateway_name,
        gateway_namespace=command.gateway_namespace,
        gateway_section_name=command.gateway_section_name,
        reuse_values=release_exists,
        timeout=command.timeout,
        dry_run=command.dry_run,
    )
    if not command.dry_run:
        _print_plan("Ready", release.display_name)


def _uninstall(command: UninstallCommand) -> None:
    """Remove the CLI-owned platform release after user services are gone."""
    helm = Helm()
    release = platform_release()
    if not helm.release_exists(release):
        _print_plan("Skip", f"{release.display_name} is not installed")
        return
    if not helm.is_cli_managed(release):
        raise DeploymentError(
            f"Helm release {release.display_name} is not managed by foretoken; "
            "use its existing Helm lifecycle"
        )

    resources = platform_service_resources(Kubectl())
    if resources:
        remaining = ", ".join(
            f"{resource.namespace}/{resource.display_name}" for resource in resources
        )
        raise DeploymentError(
            "delete Foretoken services before uninstalling the platform: "
            f"{remaining}"
        )

    _print_plan("Remove", release.display_name)
    if command.dry_run:
        return
    helm.uninstall(release, command.timeout)
    _print_plan("Removed", release.display_name)


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
