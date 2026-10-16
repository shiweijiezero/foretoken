# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken CLI entry point."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import ExitStack
from urllib.parse import urlsplit

import yaml

from foretoken.arguments import (
    ClusterCommand,
    DeleteCommand,
    DeployCommand,
    EndpointCommand,
    EvaluationCommand,
    InstallCommand,
    PerformanceCommand,
    PlotCommand,
    ProfileCommand,
    ProfileViewCommand,
    StatusCommand,
    StopCommand,
    UninstallCommand,
    parse_arguments,
)
from foretoken.cluster import run as run_cluster
from foretoken.editable import EditableDeployment
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
from foretoken.manifest import (
    DeploymentError,
    ResourceRef,
    parse_deployment,
    service_requires_execution,
)
from foretoken.platform import PlatformLifecycle
from foretoken.profiling import ProfileRun
from foretoken.profiling.viewer import view
from foretoken.progress import StartupProgress
from foretoken.service_execution import stop_deployment
from foretoken.source import (
    release_source_deployment,
    source_operation,
)
from foretoken.storage import DirectoryVolumes
from foretoken.submission import DeploymentSubmission


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
    kustomize_path: str, timeout: str, profile: ProfileCommand | None = None,
    *, editable: str | None = None, engine_sources: tuple[str, ...] = (),
) -> None:
    """Apply and wait for serving readiness, then optionally capture external traffic."""
    started = time.monotonic()
    kubectl = Kubectl()
    deployment = load_deployment(kustomize_path, kubectl)
    timeout_seconds(timeout)
    submission = DeploymentSubmission(kubectl, deployment, timeout)
    capture = None
    if profile is not None:
        # Resolve the selected model before changing the deployment.
        capture = ProfileRun(profile, deployment=deployment)
    services = [
        obj for obj in deployment.objects
        if obj.get("kind") in {"ModelService", "FrontendService"}
        and str(obj.get("apiVersion") or "").startswith("inference.foretoken.io/")
    ]
    schema = json.loads(
        kubectl.get_raw("/openapi/v3/apis/inference.foretoken.io/v1alpha1", timeout)
    )
    selection_kinds = set()
    for definition in schema["components"]["schemas"].values():
        for identity in definition.get("x-kubernetes-group-version-kind", []):
            if (
                identity["kind"] in {"ModelService", "FrontendService"}
                and "deploymentRevision"
                in definition["properties"]["spec"]["properties"]
            ):
                selection_kinds.add(identity["kind"])
    revision = uuid.uuid4().hex
    for obj in services:
        obj["spec"].setdefault("suspend", False)
        if obj.get("kind") in selection_kinds:
            obj["spec"]["deploymentRevision"] = revision
    deployment = parse_deployment(
        deployment.path, yaml.safe_dump_all(deployment.objects, sort_keys=False)
    )
    stopping = [obj for obj in services if not service_requires_execution(obj)]
    runtime_required = len(stopping) != len(services)
    namespace = deployment.namespace or "<current>"
    print(f"Applying {deployment.path} to namespace {namespace}")
    # Submit retirement before startup preparation or its locks. Inactive services
    # retain their existing storage and executable selections until they resume.
    if stopping:
        namespaces = [
            obj for obj in deployment.objects
            if obj.get("kind") == "Namespace"
            and (obj.get("metadata") or {}).get("name") == deployment.namespace
        ]
        submission.apply(yaml.safe_dump_all([*namespaces, *stopping], sort_keys=False))
    source = None
    # Explicit source options still update the checkout's future selection while idle.
    # Ready observation remains outside both submission locks.
    with ExitStack() as locks:
        if runtime_required or editable is not None or engine_sources:
            locks.enter_context(source_operation(kubectl, timeout, shared=True))
            source = EditableDeployment.discover(kubectl, editable, engine_sources)
            if source is not None:
                locks.enter_context(source.operation(deployment, timeout))
                if runtime_required:
                    prepared_at = time.monotonic()
                    source.prepare(timeout)
                    deployment = source.apply(deployment, timeout)
                    print(
                        f"Source preparation and publication completed in {time.monotonic() - prepared_at:.1f}s",
                        flush=True,
                    )
        if runtime_required:
            DirectoryVolumes(kubectl).apply(deployment, timeout, submit=submission.apply)
    print(f"Waiting up to {timeout} for Foretoken services")
    rollout_started = time.monotonic()
    with StartupProgress(kubectl, lambda line: print(line, flush=True)) as startup:
        def observe(
            resources: tuple[ResourceRef, ...], elapsed: float, budget: float = 5.0,
        ) -> None:
            """Observe readiness only while the submitted service intent remains current."""
            submission.check()
            startup.poll(resources, elapsed, budget)

        try:
            wait_for_resources(
                deployment.service_refs(),
                kubectl,
                timeout,
                report=_report_progress,
                observe=observe,
                expected_revisions={
                    ref: revision for ref in deployment.service_refs()
                    if ref.kind in selection_kinds
                },
            )
        except DeploymentError:
            # Capture the final container attempt even when the normal polling interval has not elapsed.
            startup.poll(
                deployment.service_refs(), time.monotonic() - rollout_started,
                follow_logs=False, force=True,
            )
            raise
        if source is not None and runtime_required:
            source.verify(
                deployment,
                timeout,
                observe=lambda: observe(
                    deployment.service_refs(), time.monotonic() - rollout_started
                ),
            )
    submission.check()
    print(f"Foretoken deployment completed in {time.monotonic() - started:.1f}s")
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
    release_source_deployment(kubectl, deployment, timeout)
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
        progress = selected_progress()
        _print_status(progress)
        with StartupProgress(kubectl, lambda line: print(line, flush=True)) as startup:
            startup.poll((item.resource for item in progress), 0.0, follow_logs=False)
        return

    started = time.monotonic()
    previous: dict[ResourceRef, tuple[str, str, str]] = {}
    with StartupProgress(kubectl, lambda line: print(line, flush=True)) as startup:
        while True:
            progress = selected_progress()
            elapsed = time.monotonic() - started
            for item in progress:
                signature = (item.state, item.reason, item.message)
                if previous.get(item.resource) != signature:
                    _report_progress(elapsed, item)
                    previous[item.resource] = signature
            startup.poll((item.resource for item in progress), elapsed)
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


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch Foretoken deployment, status, and benchmark commands."""
    command = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        if isinstance(command, ClusterCommand):
            run_cluster(command)
        elif isinstance(command, InstallCommand):
            oci_registry = command.oci_registry or os.environ.get(
                "FORETOKEN_OCI_REGISTRY"
            )
            with source_operation(
                Kubectl(), command.timeout, installing=command.editable is not None
            ):
                PlatformLifecycle(oci_registry).install(command)
        elif isinstance(command, UninstallCommand):
            with source_operation(Kubectl(), command.timeout):
                PlatformLifecycle().uninstall(command)
        elif isinstance(command, DeployCommand):
            _deploy(
                command.kustomize_path, command.timeout, command.profile,
                editable=command.editable, engine_sources=command.engine_sources,
            )
        elif isinstance(command, DeleteCommand):
            with source_operation(Kubectl(), command.timeout):
                _delete(command.kustomize_path, command.timeout)
        elif isinstance(command, StopCommand):
            stop_deployment(command)
        elif isinstance(command, StatusCommand):
            _status(command.kustomize_path, command.namespace, command.watch)
        elif isinstance(command, EndpointCommand):
            _endpoint(
                command.kustomize_path,
                command.timeout,
                command.host,
            )
        elif isinstance(command, PerformanceCommand):
            # Load benchmark frameworks only for benchmark commands.
            from benchmarks.main import main as benchmark_main

            benchmark_main(command.arguments)
        elif isinstance(command, EvaluationCommand):
            from benchmarks.evaluation import main as evaluation_main

            evaluation_main(command.arguments)
        elif isinstance(command, PlotCommand):
            from benchmarks.plot import main as plot_main

            plot_main(command.arguments)
        elif isinstance(command, ProfileViewCommand):
            view(command)
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
