# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Public command definitions and argument parsing for the Foretoken CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from foretoken import package_version


@dataclass(frozen=True)
class InstallCommand:
    """Install or update the Foretoken platform Helm release."""

    values: tuple[str, ...]
    editable: str | None
    registry: str | None
    oci_registry: str | None
    prometheus: str | None
    frontend_mode: str | None
    gateway_name: str
    gateway_namespace: str
    gateway_section_name: str
    timeout: str


@dataclass(frozen=True)
class UninstallCommand:
    """Remove the Foretoken platform Helm release."""

    timeout: str


@dataclass(frozen=True)
class DeployCommand:
    """Apply one Kustomize deployment and wait for serving readiness."""

    kustomize_path: str
    timeout: str
    profile: ProfileCommand | None = None


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
class PerformanceCommand:
    """Forward performance benchmark arguments to the benchmark module."""

    arguments: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationCommand:
    """Forward model evaluation arguments to the benchmark module."""

    arguments: tuple[str, ...]


@dataclass(frozen=True)
class ProfileCommand:
    """Describe one runtime-owned capture requested by deploy or perf."""

    kustomize_path: str
    model: str | None
    profile_duration: str
    profile_engine: str
    timeout: str


@dataclass(frozen=True)
class ProfileViewCommand:
    """Browse capture directories in all or one selected Kubernetes namespace."""

    namespace: str | None
    timeout: str


def add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared optional capture controls to deploy and perf."""
    parser.add_argument(
        "--profile", action="store_true",
        help="Capture a profile during this operation",
    )
    parser.add_argument(
        "--profile-engine", choices=("pytorch", "nsight", "mctracer"),
        help="Required with --profile; native profiler",
    )
    parser.add_argument(
        "--profile-duration",
        help="Required with --profile; maximum recording time, e.g. 15s",
    )


def validate_profile_arguments(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> None:
    """Require a complete capture selection before executing deploy or perf."""
    if arguments.profile and not (arguments.profile_engine and arguments.profile_duration):
        parser.error("--profile requires --profile-engine and --profile-duration")
    if not arguments.profile and (arguments.profile_engine or arguments.profile_duration):
        parser.error("--profile-engine and --profile-duration require --profile")


ParsedCommand = (
    InstallCommand
    | UninstallCommand
    | DeployCommand
    | DeleteCommand
    | StatusCommand
    | EndpointCommand
    | PerformanceCommand
    | EvaluationCommand
    | ProfileViewCommand
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
        description=(
            "Install the Kubernetes control plane, deploy model services, "
            "capture profiles, run performance benchmarks, and evaluate models"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser(
        "install",
        help="Install or update the Foretoken Kubernetes control plane",
        description=(
            "Install or update Foretoken CRDs and the controller, discover the cluster "
            "LoadBalancer, configure shared monitoring, and create Gateway resources "
            "when Gateway mode is selected. "
            "Model services are deployed separately with 'foretoken deploy'."
        ),
    )
    install.add_argument(
        "-e",
        "--editable",
        metavar="PATH",
        help="build Foretoken images from this source root",
    )
    install.add_argument(
        "--registry",
        metavar="REGISTRY",
        help="registry used to distribute source images to remote clusters",
    )
    install.add_argument(
        "--oci-registry",
        metavar="REGISTRY",
        help=(
            "explicit registry prefix for default platform images and Helm charts; "
            "defaults to FORETOKEN_OCI_REGISTRY (otherwise compares supported "
            "public sources automatically; explicit image choices are preserved)"
        ),
    )
    install.add_argument(
        "-f",
        "--values",
        action="append",
        metavar="PATH",
        help=(
            "Helm values for images, runtime, hardware, or a managed LoadBalancer "
            "address pool; may be repeated"
        ),
    )
    install.add_argument(
        "--prometheus",
        metavar="NAMESPACE/NAME",
        help="existing compatible Prometheus when automatic discovery is ambiguous",
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

    uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove the Foretoken platform release",
    )
    _add_wait_timeout_argument(uninstall, "platform removal")

    deploy = subparsers.add_parser(
        "deploy",
        help="Apply a Kustomize deployment and wait for serving readiness",
    )
    deploy.add_argument(
        "kustomize_path",
        metavar="PATH",
        help="Kustomize root containing one frontend and one or more models",
    )
    _add_wait_timeout_argument(deploy, "serving readiness or capture completion")
    add_profile_arguments(deploy)
    deploy.add_argument("--model", help="model to profile when PATH contains several models")

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

    profile = subparsers.add_parser(
        "profile", help="Browse retained performance captures"
    )
    profile_actions = profile.add_subparsers(dest="profile_action", required=True)
    view = profile_actions.add_parser(
        "view", help="Print a local URL for current and historical captures",
        description=(
            "Run on the browser's computer using a kubeconfig for the target cluster. "
            "Ctrl+C closes the viewer."
        ),
    )
    view.add_argument(
        "-n", "--namespace",
        help="limit capture directories to one namespace (default: all namespaces)",
    )
    _add_wait_timeout_argument(view, "capture storage readiness")

    subparsers.add_parser(
        "perf",
        add_help=False,
        help="Measure Foretoken or other OpenAI-compatible model services",
    )
    subparsers.add_parser(
        "eval",
        add_help=False,
        help="Score model answers or compare reference and candidate distributions",
    )
    return parser


def parse_arguments(argv: Sequence[str]) -> ParsedCommand:
    """Parse CLI arguments into the command consumed by the execution layer."""
    arguments = tuple(argv)
    if arguments and arguments[0] == "perf":
        return PerformanceCommand(arguments[1:])
    if arguments and arguments[0] == "eval":
        return EvaluationCommand(arguments[1:])

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
        if parsed_args.registry and not parsed_args.editable:
            parser.error("--registry requires --editable PATH")
        return InstallCommand(
            tuple(parsed_args.values or ()),
            parsed_args.editable,
            parsed_args.registry,
            parsed_args.oci_registry,
            parsed_args.prometheus,
            parsed_args.frontend_mode,
            parsed_args.gateway_name,
            parsed_args.gateway_namespace,
            parsed_args.gateway_section_name,
            parsed_args.timeout,
        )
    if parsed_args.command == "uninstall":
        return UninstallCommand(parsed_args.timeout)
    if parsed_args.command == "deploy":
        validate_profile_arguments(parser, parsed_args)
        if parsed_args.model and not parsed_args.profile:
            parser.error("deploy --model requires --profile")
        capture = (
            ProfileCommand(
                parsed_args.kustomize_path, parsed_args.model,
                parsed_args.profile_duration, parsed_args.profile_engine,
                parsed_args.timeout,
            )
            if parsed_args.profile else None
        )
        return DeployCommand(parsed_args.kustomize_path, parsed_args.timeout, capture)
    if parsed_args.command == "delete":
        return DeleteCommand(parsed_args.kustomize_path, parsed_args.timeout)
    if parsed_args.command == "profile":
        return ProfileViewCommand(parsed_args.namespace, parsed_args.timeout)
    if parsed_args.command == "status":
        if bool(parsed_args.kustomize_path) == bool(parsed_args.namespace):
            parser.error("status requires either PATH or --namespace")
        return StatusCommand(
            parsed_args.kustomize_path,
            parsed_args.namespace,
            parsed_args.watch,
        )
    return EndpointCommand(
        parsed_args.kustomize_path,
        parsed_args.timeout,
        parsed_args.host,
    )
