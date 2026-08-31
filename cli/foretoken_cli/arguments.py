# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Public command definitions and argument parsing for the Foretoken CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass


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
    DeployCommand | DeleteCommand | StatusCommand | EndpointCommand | BenchCommand
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
        description="Deploy, inspect, and benchmark Foretoken services",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    if parsed_args.command == "deploy":
        return DeployCommand(parsed_args.kustomize_path, parsed_args.timeout)
    if parsed_args.command == "delete":
        return DeleteCommand(parsed_args.kustomize_path, parsed_args.timeout)
    if parsed_args.command == "status":
        if bool(parsed_args.kustomize_path) == bool(parsed_args.namespace):
            parser.error("status requires either PATH or --namespace")
        return StatusCommand(parsed_args.kustomize_path, parsed_args.namespace, parsed_args.watch)
    return EndpointCommand(parsed_args.kustomize_path, parsed_args.timeout, parsed_args.host)
