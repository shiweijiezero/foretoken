# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken CLI entry point."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from foretoken_cli.kubernetes import (
    Kubectl,
    ResourceProgress,
    load_deployment,
    namespace_progress,
    read_progress,
    timeout_seconds,
    wait_for_resources,
)
from foretoken_cli.manifest import DeploymentError, ResourceRef

_DEFAULT_TIMEOUT = "10m"


def _parser() -> argparse.ArgumentParser:
    """Build the stable top-level command surface owned by the CLI package."""
    parser = argparse.ArgumentParser(
        prog="foretoken",
        description="Deploy, inspect, and benchmark Foretoken services",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    deploy = subparsers.add_parser(
        "deploy",
        help="Apply a Kustomize deployment and wait for serving readiness",
    )
    deploy.add_argument(
        "-k",
        "--kustomize",
        required=True,
        metavar="PATH",
        help="Kustomize root containing one frontend and one or more models",
    )
    deploy.add_argument(
        "--timeout",
        default=_DEFAULT_TIMEOUT,
        help=f"maximum readiness wait (default: {_DEFAULT_TIMEOUT})",
    )

    status = subparsers.add_parser(
        "status",
        help="Show Foretoken service readiness",
    )
    source = status.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-k",
        "--kustomize",
        metavar="PATH",
        help="inspect services rendered by this Kustomize root",
    )
    source.add_argument(
        "-n",
        "--namespace",
        metavar="NAMESPACE",
        help="inspect all Foretoken services in this namespace",
    )
    status.add_argument(
        "--watch",
        action="store_true",
        help="print service state changes until interrupted",
    )

    subparsers.add_parser(
        "bench",
        add_help=False,
        help="Benchmark a Foretoken or OpenAI-compatible service",
    )
    return parser


def _deployment_resources(path_value: str, kubectl: Kubectl) -> tuple[ResourceRef, ...]:
    """Render a Kustomize root and return its user-facing service resources."""
    return load_deployment(path_value, kubectl).service_refs()


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


def _deploy(namespace_path: str, timeout: str) -> None:
    """Apply one Kustomize deployment and wait for current-generation readiness."""
    kubectl = Kubectl()
    deployment = load_deployment(namespace_path, kubectl)
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


def _status(path_value: str | None, namespace: str | None, watch: bool) -> None:
    """Inspect a rendered deployment or all services in one namespace."""
    kubectl = Kubectl()
    deployment_resources = (
        _deployment_resources(path_value, kubectl)
        if path_value is not None
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


def _bench(argv: Sequence[str]) -> None:
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
    benchmark_main(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch Foretoken deployment, status, and benchmark commands."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "bench":
        _bench(arguments)
        return

    parser = _parser()
    command = parser.parse_args(arguments)
    try:
        if command.command == "deploy":
            _deploy(command.kustomize, command.timeout)
        elif command.command == "status":
            _status(command.kustomize, command.namespace, command.watch)
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
