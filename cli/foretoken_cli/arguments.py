# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Public command definitions and argument parsing for the Foretoken CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

_DEFAULT_TIMEOUT = "10m"


@dataclass(frozen=True)
class DeployCommand:
    """Apply one Kustomize deployment and wait for serving readiness."""

    kustomize_path: str
    timeout: str


@dataclass(frozen=True)
class StatusCommand:
    """Inspect services selected by Kustomize configuration or namespace."""

    kustomize_path: str | None
    namespace: str | None
    watch: bool


@dataclass(frozen=True)
class BenchCommand:
    """Forward benchmark arguments to the optional benchmark module."""

    arguments: tuple[str, ...]


ParsedCommand = DeployCommand | StatusCommand | BenchCommand


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
        "-k",
        "--kustomize",
        required=True,
        dest="kustomize_path",
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
        dest="kustomize_path",
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


def parse_arguments(argv: Sequence[str]) -> ParsedCommand:
    """Parse CLI arguments into the command consumed by the execution layer."""
    arguments = tuple(argv)
    if arguments and arguments[0] == "bench":
        return BenchCommand(arguments)

    parsed = _build_parser().parse_args(arguments)
    if parsed.command == "deploy":
        return DeployCommand(parsed.kustomize_path, parsed.timeout)
    return StatusCommand(parsed.kustomize_path, parsed.namespace, parsed.watch)
