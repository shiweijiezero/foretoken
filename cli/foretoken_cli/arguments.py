# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Public command definitions and argument parsing for the Foretoken CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import version


@dataclass(frozen=True)
class InstallCommand:
    """Install or update the Foretoken platform Helm release."""

    values: tuple[str, ...]
    frontend_mode: str | None
    gateway_name: str
    gateway_namespace: str
    gateway_section_name: str
    timeout: str
    dry_run: bool


@dataclass(frozen=True)
class UninstallCommand:
    """Remove the Foretoken platform Helm release."""

    timeout: str
    dry_run: bool


@dataclass(frozen=True)
class DeployCommand:
    """Apply one Kustomize deployment and wait for serving readiness."""

    kustomize_path: str
    timeout: str


@dataclass(frozen=True)
class DeleteCommand:
    """Delete the resources rendered by one Kustomize deployment."""

    kustomize_path: str
    timeout: str


@dataclass(frozen=True)
class StatusCommand:
    """Inspect services selected by Kustomize configuration or namespace."""

    kustomize_path: str | None
    namespace: str | None
    watch: bool


@dataclass(frozen=True)
class EndpointCommand:
    """Resolve the public frontend endpoint for one Kustomize deployment."""

    kustomize_path: str
    timeout: str
    host: bool


@dataclass(frozen=True)
class BenchCommand:
    """Forward benchmark arguments to the optional benchmark module."""

    arguments: tuple[str, ...]


ParsedCommand = (
    InstallCommand
    | UninstallCommand
    | DeployCommand
    | DeleteCommand
    | StatusCommand
    | EndpointCommand
    | BenchCommand
)


def _add_wait_timeout_argument(
    parser: argparse.ArgumentParser, target: str
) -> None:
    """Add the shared bounded-wait option for a command target."""
    parser.add_argument(
        "--timeout",
        default="10m",
        help=f"maximum {target} wait (default: %(default)s)",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the stable top-level command surface owned by the CLI package."""
    parser = argparse.ArgumentParser(
        prog="foretoken",
        description="Install the platform, deploy services, and run benchmarks",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('foretoken-cli')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser(
        "install",
        help="Install or update the Foretoken platform",
    )
    install.add_argument(
        "-f",
        "--values",
        action="append",
        metavar="PATH",
        help="Helm values file; may be repeated",
    )
    install.add_argument(
        "--frontend-mode",
        choices=("local", "gateway"),
        help="frontend access mode; new releases default to local",
    )
    install.add_argument(
        "--gateway-name",
        default="",
        metavar="NAME",
        help="existing Gateway name",
    )
    install.add_argument(
        "--gateway-namespace",
        default="",
        metavar="NAMESPACE",
        help="existing Gateway namespace",
    )
    install.add_argument(
        "--gateway-section-name",
        default="",
        metavar="NAME",
        help="listener name on an existing Gateway",
    )
    _add_wait_timeout_argument(install, "platform readiness")
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="show the installation plan without changing the cluster",
    )

    uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove the Foretoken platform release",
    )
    _add_wait_timeout_argument(uninstall, "platform removal")
    uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="show the removal plan without changing the cluster",
    )

    deploy = subparsers.add_parser(
        "deploy",
        help="Apply a Kustomize deployment and wait for serving readiness",
    )
    deploy.add_argument(
        "kustomize_path",
        metavar="PATH",
        help="Kustomize root containing one frontend and one or more models",
    )
    _add_wait_timeout_argument(deploy, "serving readiness")

    delete = subparsers.add_parser(
        "delete",
        help="Delete the resources rendered by a Kustomize deployment",
    )
    delete.add_argument(
        "kustomize_path",
        metavar="PATH",
        help="Kustomize root whose resources should be deleted",
    )
    _add_wait_timeout_argument(delete, "resource deletion")

    status = subparsers.add_parser(
        "status",
        help="Show Foretoken service readiness",
    )
    status.add_argument(
        "kustomize_path",
        nargs="?",
        metavar="PATH",
        help="Kustomize root whose services should be inspected",
    )
    status.add_argument(
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

    endpoint = subparsers.add_parser(
        "endpoint",
        help="Wait for and print a deployment's public frontend endpoint",
    )
    endpoint.add_argument(
        "kustomize_path",
        metavar="PATH",
        help="Kustomize root containing one frontend",
    )
    _add_wait_timeout_argument(endpoint, "endpoint")
    endpoint.add_argument(
        "--host",
        action="store_true",
        help="print the HTTP Host value instead of the URL",
    )

    subparsers.add_parser(
        "bench",
        add_help=False,
        help="Benchmark a Foretoken or OpenAI-compatible service",
    )
    return parser


def parse_arguments(argv: Sequence[str]) -> ParsedCommand:
    """Parse CLI arguments into the command consumed by the execution layer."""
    arguments = tuple(argv)
    if arguments and arguments[0] == "bench":
        return BenchCommand(arguments)

    parser = _build_parser()
    parsed_args = parser.parse_args(arguments)
    if parsed_args.command == "install":
        reused_gateway_arguments = any(
            (
                parsed_args.gateway_name,
                parsed_args.gateway_namespace,
                parsed_args.gateway_section_name,
            )
        )
        if parsed_args.frontend_mode != "gateway" and reused_gateway_arguments:
            parser.error("Gateway options require --frontend-mode gateway")
        if (
            parsed_args.frontend_mode == "gateway"
            and reused_gateway_arguments
            and not (parsed_args.gateway_name and parsed_args.gateway_namespace)
        ):
            parser.error(
                "reusing a Gateway requires both --gateway-name and "
                "--gateway-namespace"
            )
        return InstallCommand(
            tuple(parsed_args.values or ()),
            parsed_args.frontend_mode,
            parsed_args.gateway_name,
            parsed_args.gateway_namespace,
            parsed_args.gateway_section_name,
            parsed_args.timeout,
            parsed_args.dry_run,
        )
    if parsed_args.command == "uninstall":
        return UninstallCommand(parsed_args.timeout, parsed_args.dry_run)
    if parsed_args.command == "deploy":
        return DeployCommand(parsed_args.kustomize_path, parsed_args.timeout)
    if parsed_args.command == "delete":
        return DeleteCommand(parsed_args.kustomize_path, parsed_args.timeout)
    if parsed_args.command == "status":
        if bool(parsed_args.kustomize_path) == bool(parsed_args.namespace):
            parser.error("status requires either PATH or --namespace")
        return StatusCommand(parsed_args.kustomize_path, parsed_args.namespace, parsed_args.watch)
    return EndpointCommand(parsed_args.kustomize_path, parsed_args.timeout, parsed_args.host)
