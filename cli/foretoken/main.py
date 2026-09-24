# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken CLI entry point."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

from foretoken.arguments import (
    DeleteCommand,
    DeployCommand,
    EndpointCommand,
    EvaluationCommand,
    InstallCommand,
    PerformanceCommand,
    ProfileCommand,
    ProfileViewCommand,
    StatusCommand,
    UninstallCommand,
    parse_arguments,
)
from foretoken.kubernetes import (
    Kubectl,
    ResourceProgress,
    load_deployment,
    namespace_progress,
    read_progress,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.platform import PlatformLifecycle
from foretoken.profiling import ProfileRun
from foretoken.storage import DirectoryVolumes


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


def _deploy(
    kustomize_path: str, timeout: str, profile: ProfileCommand | None = None
) -> None:
    """Apply and wait for serving readiness, then optionally capture external traffic."""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    timeout_seconds(timeout)
    capture = None
    if profile is not None:
        # Resolve the selected model before changing the deployment.
        capture = ProfileRun(profile, deployment=deployment)
    namespace = deployment.namespace or "<current>"
    print(f"Applying {deployment.path} to namespace {namespace}")
    DirectoryVolumes(kubectl).apply(deployment, timeout)
    print(f"Waiting up to {timeout} for Foretoken services")
    started = time.monotonic()
    wait_for_resources(
        deployment.service_refs(),
        kubectl,
        timeout,
        report=_report_progress,
    )
    print(f"Foretoken deployment is ready in {time.monotonic() - started:.1f}s")
    if capture is not None:
        try:
            capture.start()
            capture.wait()
        except KeyboardInterrupt:
            capture.cancel()
            raise


def _delete(kustomize_path: str, timeout: str) -> None:
    """Delete one rendered deployment and wait for resource termination."""
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    timeout_seconds(timeout)
    namespace = deployment.namespace or "<current>"
    print(f"Deleting {deployment.path} from namespace {namespace}")
    DirectoryVolumes(kubectl).delete(deployment, timeout)
    print("Foretoken deployment deleted")


def _status(kustomize_path: str | None, namespace: str | None, watch: bool) -> None:
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
    if deployment.frontend is None:
        raise DeploymentError(
            "deployment has no FrontendService; forward a ModelGroup Service instead"
        )
    print("Waiting for the frontend endpoint", file=sys.stderr, flush=True)
    endpoint = resolve_frontend_endpoint(deployment, kubectl, timeout)
    if host:
        print(endpoint.routing_host or urlsplit(endpoint.url).netloc)
        return
    print(endpoint.url)


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch Foretoken deployment, status, and benchmark commands."""
    command = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        if isinstance(command, InstallCommand):
            oci_registry = command.oci_registry or os.environ.get(
                "FORETOKEN_OCI_REGISTRY"
            )
            PlatformLifecycle(oci_registry).install(command)
        elif isinstance(command, UninstallCommand):
            PlatformLifecycle().uninstall(command)
        elif isinstance(command, DeployCommand):
            _deploy(command.kustomize_path, command.timeout, command.profile)
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
        elif isinstance(command, PerformanceCommand):
            from benchmarks.main import main as benchmark_main

            benchmark_main(command.arguments)
        elif isinstance(command, EvaluationCommand):
            from benchmarks.evaluation import main as evaluation_main

            evaluation_main(command.arguments)
        elif isinstance(command, ProfileViewCommand):
            from foretoken.profiling.viewer import view

            view(command)
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
