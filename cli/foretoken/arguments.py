# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Public command definitions and argument parsing for the Foretoken CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from foretoken import package_version
from foretoken.errors import DeploymentError


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
    grafana_auth: str | None = None
    engine_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallCommand:
    """Remove the Foretoken platform Helm release."""

    timeout: str


@dataclass(frozen=True)
class ClusterCommand:
    """Create or remove a locally managed development cluster."""

    action: str
    kind: str
    name: str
    gpus: str | None = None
    config: str | None = None


@dataclass(frozen=True)
class DeployCommand:
    """Apply one Kustomize deployment and wait for serving readiness."""

    kustomize_path: str
    timeout: str
    profile: ProfileCommand | None = None
    editable: str | None = None
    engine_sources: tuple[str, ...] = ()
    name: str | None = None


@dataclass(frozen=True)
class DeleteCommand:
    """Retire a named deployment instance or the instance selected by a Kustomize root."""

    target: str
    timeout: str
    name: str | None = None


@dataclass(frozen=True)
class StopCommand:
    """Suspend a selected deployment instance while preserving its configuration and storage."""

    target: str
    timeout: str
    name: str | None = None


@dataclass(frozen=True)
class StatusCommand:
    """List deployment instances or inspect one selected instance and its services."""

    target: str | None
    watch: bool
    name: str | None = None


@dataclass(frozen=True)
class EndpointCommand:
    """Resolve the public frontend endpoint for a selected deployment instance."""

    target: str
    timeout: str
    host: bool
    name: str | None = None


@dataclass(frozen=True)
class PerformanceCommand:
    """Forward performance benchmark arguments to the benchmark module."""

    arguments: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationCommand:
    """Forward model evaluation arguments to the benchmark module."""

    arguments: tuple[str, ...]


@dataclass(frozen=True)
class PlotCommand:
    """Forward saved-result plotting arguments without loading benchmark dependencies."""

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


ParsedCommand = (
    InstallCommand
    | UninstallCommand
    | ClusterCommand
    | DeployCommand
    | DeleteCommand
    | StopCommand
    | StatusCommand
    | EndpointCommand
    | PerformanceCommand
    | EvaluationCommand
    | PlotCommand
    | ProfileViewCommand
)


def parse_arguments(argv: Sequence[str]) -> ParsedCommand:
    """Parse CLI arguments into the command consumed by the execution layer."""
    arguments = tuple(argv)
    if arguments and arguments[0] == "perf":
        return PerformanceCommand(arguments[1:])
    if arguments and arguments[0] == "eval":
        return EvaluationCommand(arguments[1:])
    if arguments and arguments[0] == "plot":
        return PlotCommand(arguments[1:])

    parser = _build_parser()
    parsed_args = parser.parse_args(arguments)
    if parsed_args.command == "cluster":
        if parsed_args.cluster_action == "create" and parsed_args.kind == "k3d" and not parsed_args.gpus:
            parser.error("cluster create k3d requires --gpus INDICES")
        return ClusterCommand(
            parsed_args.cluster_action,
            parsed_args.kind,
            parsed_args.name,
            getattr(parsed_args, "gpus", None),
            getattr(parsed_args, "config", None),
        )
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
        if parsed_args.engine_source and not parsed_args.editable:
            parser.error("--engine-source requires --editable PATH")
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
            parsed_args.grafana_auth,
            tuple(parsed_args.engine_source or ()),
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
        return DeployCommand(
            parsed_args.kustomize_path, parsed_args.timeout, capture,
            parsed_args.editable, tuple(parsed_args.engine_source or ()), parsed_args.name,
        )
    if parsed_args.command == "delete":
        return DeleteCommand(parsed_args.target, parsed_args.timeout, parsed_args.name)
    if parsed_args.command == "stop":
        return StopCommand(parsed_args.target, parsed_args.timeout, parsed_args.name)
    if parsed_args.command == "profile":
        return ProfileViewCommand(parsed_args.namespace, parsed_args.timeout)
    if parsed_args.command == "status":
        if parsed_args.name is not None and parsed_args.target is None:
            parser.error("status --name requires a Kustomize PATH; otherwise pass the instance name directly")
        return StatusCommand(parsed_args.target, parsed_args.watch, parsed_args.name)
    return EndpointCommand(parsed_args.target, parsed_args.timeout, parsed_args.host, parsed_args.name)


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


def _add_wait_timeout_argument(
    parser: argparse.ArgumentParser, target: str
) -> None:
    """Add the shared bounded-wait option for a command target."""
    parser.add_argument(
        "--timeout",
        default="10m",
        help=f"maximum {target} wait (default: %(default)s)",
    )


def _namespace_name(value: str) -> str:
    """Reject empty selections before Kubernetes tools can substitute the current context's namespace."""
    if not value.strip():
        raise argparse.ArgumentTypeError("namespace cannot be empty")
    return value


def _instance_name(value: str) -> str:
    """Apply the frontend Service-name rule to an explicit CLI instance selection."""
    from foretoken.deployment.manifest import validate_instance_name

    try:
        return validate_instance_name(value)
    except DeploymentError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


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

    cluster = subparsers.add_parser(
        "cluster",
        help="Create or remove a local kind or k3d cluster",
    )
    cluster_actions = cluster.add_subparsers(dest="cluster_action", required=True)
    for action in ("create", "delete"):
        cluster_action = cluster_actions.add_parser(
            action,
            help=(
                "Create a local cluster" if action == "create"
                else "Delete a local cluster"
            ),
        )
        cluster_action.add_argument("kind", choices=("kind", "k3d"))
        cluster_action.add_argument(
            "--name",
            default="foretoken-dev",
            metavar="NAME",
            help="name of the local cluster",
        )
        if action == "create":
            cluster_action.add_argument(
                "--gpus",
                metavar="INDICES",
                help="GPU indices for k3d, for example 0 or 0,1",
            )
            cluster_action.add_argument(
                "--config",
                metavar="PATH",
                help="kind or k3d configuration file",
            )

    install = subparsers.add_parser(
        "install",
        help="Prepare Foretoken's shared Kubernetes infrastructure",
        description=(
            "Prepare Foretoken CRDs, discover the cluster LoadBalancer, configure shared "
            "metrics and persistent logs, and create Gateway resources when Gateway mode "
            "is selected. Deploy independent inference instances with 'foretoken deploy'."
        ),
    )
    install.add_argument(
        "-e",
        "--editable",
        metavar="PATH",
        help="build shared platform and deployment application defaults from this source directory",
    )
    install.add_argument(
        "--engine-source", action="append", metavar="[ENGINE=]PATH",
        help="select vLLM source; repeat with vllm-metax=PATH for its plugin (requires -e)",
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
            "Helm values for images, runtime, hardware, logging, build caches, or a managed LoadBalancer "
            "address pool; may be repeated"
        ),
    )
    install.add_argument(
        "--prometheus",
        metavar="NAMESPACE/NAME",
        help="existing compatible Prometheus when automatic discovery is ambiguous",
    )
    install.add_argument(
        "--grafana-auth",
        choices=("anonymous", "password"),
        help="managed Grafana access; overrides values, new installations allow anonymous viewing",
    )
    install.add_argument(
        "--frontend-mode",
        choices=("local", "gateway"),
        help="gateway shares one entry point; local uses a separate LoadBalancer Service per instance",
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
    deploy.add_argument(
        "-e", "--editable", metavar="PATH",
        help=(
            "build controller and serving applications from this source directory for the selected instance "
            "(requires a source-installed platform)"
        ),
    )
    deploy.add_argument(
        "--engine-source", action="append", metavar="[ENGINE=]PATH",
        help="select an engine source directory; repeat for vllm-metax",
    )
    deploy.add_argument(
        "--name", type=_instance_name, metavar="NAME",
        help="deployment instance name (default: the rendered frontend name)",
    )
    _add_wait_timeout_argument(deploy, "serving readiness or capture completion")
    add_profile_arguments(deploy)
    deploy.add_argument("--model", help="model to profile when PATH contains several models")

    delete = subparsers.add_parser(
        "delete",
        help="Delete a deployment instance",
    )
    delete.add_argument(
        "target", metavar="NAME_OR_PATH",
        help="deployment instance name or Kustomize root to delete",
    )
    delete.add_argument("--name", type=_instance_name, metavar="NAME", help="instance selected by PATH")
    _add_wait_timeout_argument(delete, "resource deletion")

    stop = subparsers.add_parser(
        "stop", help="Stop a deployment while keeping its configuration and storage",
    )
    stop.add_argument("target", metavar="NAME_OR_PATH", help="deployment instance name or Kustomize root to stop")
    stop.add_argument("--name", type=_instance_name, metavar="NAME", help="instance selected by PATH")
    _add_wait_timeout_argument(stop, "service shutdown")

    status = subparsers.add_parser(
        "status",
        help="List deployment instances or inspect their readiness",
    )
    status.add_argument(
        "target", nargs="?", metavar="NAME_OR_PATH",
        help="deployment instance name or Kustomize root (default: list instances)",
    )
    status.add_argument("--name", type=_instance_name, metavar="NAME", help="instance selected by PATH")
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
        "target", metavar="NAME_OR_PATH",
        help="deployment instance name or Kustomize root containing one frontend",
    )
    endpoint.add_argument("--name", type=_instance_name, metavar="NAME", help="instance selected by PATH")
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
        "-n", "--namespace", type=_namespace_name,
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
    subparsers.add_parser(
        "plot", add_help=False,
        help="Export publication figures and tables from saved benchmark results",
    )
    return parser
