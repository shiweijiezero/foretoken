# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Cluster LoadBalancer discovery and CLI-managed MetalLB lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yaml

from foretoken.kubernetes import Kubectl, resource_ref
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.platform.helm import Helm
from foretoken.platform.types import LoadBalancerConfig, ReleaseRef


@dataclass(frozen=True)
class _MetalLBKind:
    """One MetalLB configuration kind addressed by manifest kind and API resource."""

    kind: str
    resource: str


_METALLB_API_GROUP = "metallb.io"
_METALLB_API_VERSION = "metallb.io/v1beta1"
_ADDRESS_POOL = _MetalLBKind("IPAddressPool", "ipaddresspools.metallb.io")
_L2_ADVERTISEMENT = _MetalLBKind("L2Advertisement", "l2advertisements.metallb.io")
_BGP_ADVERTISEMENT = _MetalLBKind("BGPAdvertisement", "bgpadvertisements.metallb.io")
# Foretoken configures Layer 2 only: one pool and one advertisement, both named
# after the managed release and carrying the management label.
_MANAGED_KINDS = (_ADDRESS_POOL, _L2_ADVERTISEMENT)


@dataclass(frozen=True)
class LoadBalancerPlan:
    """One resolved cluster LoadBalancer lifecycle decision."""

    release: ReleaseRef
    config: LoadBalancerConfig
    action: str
    detail: str
    install: bool = False
    blocking: bool = False


@dataclass(frozen=True)
class _ClusterObservation:
    """Read-only evidence about implementations that may serve default Services."""

    default_services: tuple[ResourceRef, ...]
    allocated_service: ResourceRef | None
    metallb_api: bool
    metallb_resources: tuple[ResourceRef, ...]
    configured_metallb_namespaces: tuple[str, ...]
    k3s_service_lb: str
    cloud_provider_metadata: bool


class LoadBalancerLifecycle:
    """Own LoadBalancer discovery and the optional managed MetalLB release."""

    def __init__(self, helm: Helm, kubectl: Kubectl) -> None:
        self._helm = helm
        self._kubectl = kubectl

    def resolve_install(self, config: LoadBalancerConfig) -> LoadBalancerPlan:
        """Choose reuse, managed installation, or report what could not be confirmed."""
        helm = self._helm
        release = helm.metallb_release()
        release_exists = helm.release_exists(release)
        # A release under the managed name that another tool owns is only an
        # obstacle when a managed installation is requested; otherwise it is
        # observed like any other implementation.
        if release_exists and not helm.is_cli_managed(release):
            if config.managed_addresses:
                return LoadBalancerPlan(
                    release,
                    config,
                    "Needs configuration",
                    f"Helm release {release.display_name} is not managed by "
                    "foretoken; use its existing Helm lifecycle",
                    blocking=True,
                )
            release_exists = False

        try:
            observation = self._observe_cluster()
        except DeploymentError as exc:
            detail = f"cluster capability could not be inspected: {exc}"
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration" if config.managed_addresses else "Not verified",
                detail,
                blocking=bool(config.managed_addresses),
            )

        # The managed release stores its pool, so an interrupted or partially
        # upgraded installation resumes without asking for the addresses again.
        if release_exists and not config.managed_addresses:
            config = helm.stored_load_balancer_config(release)
        if not config.managed_addresses:
            if release_exists:
                return LoadBalancerPlan(
                    release,
                    config,
                    "Needs configuration",
                    f"CLI-managed MetalLB {release.display_name} has no stored "
                    "address pool; provide loadBalancer.managedAddresses",
                )
            return self._automatic_plan(release, config, observation)

        conflict = self._managed_resource_collision(release) or (
            "" if release_exists else _managed_install_conflict(observation)
        )
        if conflict:
            return LoadBalancerPlan(
                release, config, "Needs configuration", conflict, blocking=True
            )
        return LoadBalancerPlan(
            release,
            config,
            "Upgrade" if release_exists else "Install",
            f"{release.display_name} with {len(config.managed_addresses)} explicit Layer 2 address range(s)",
            install=True,
        )

    def apply(self, plan: LoadBalancerPlan, timeout: str) -> None:
        """Install MetalLB and apply only the address resources owned by Foretoken."""
        if not plan.install:
            return
        self._helm.install_metallb(plan.release, plan.config, timeout)
        self._kubectl.wait_for_crds(
            tuple(kind.resource for kind in _MANAGED_KINDS), timeout
        )
        self._kubectl.apply(
            _managed_configuration(
                plan.release,
                self._helm.management_label,
                plan.config.managed_addresses,
            )
        )

    def finish_uninstall(self, timeout: str) -> tuple[str, str] | None:
        """Remove managed MetalLB only when no external Service or config needs it."""
        helm = self._helm
        release = helm.metallb_release()
        if not helm.release_exists(release):
            return None
        if not helm.is_cleanup_managed(release):
            return "Preserve", f"{release.display_name} is externally managed"

        try:
            observation = self._observe_cluster()
            external_config = _external_metallb_resources(
                observation.metallb_resources, release
            )
            if external_config:
                names = ", ".join(resource.display_name for resource in external_config)
                return "Preserve", f"external MetalLB configuration remains: {names}"
            if observation.default_services:
                names = ", ".join(
                    f"{resource.namespace}/{resource.display_name}"
                    for resource in observation.default_services
                )
                return "Preserve", f"default LoadBalancer Services remain: {names}"
            collision = self._managed_resource_collision(release)
            if collision:
                return "Preserve", collision
        except DeploymentError as exc:
            return "Preserve", f"dependency check failed: {exc}"

        if self._managed_objects(release):
            self._kubectl.delete(
                _managed_configuration(release, helm.management_label, ()), timeout
            )
        helm.uninstall(release, timeout)
        return "Removed", release.display_name

    def _automatic_plan(
        self,
        release: ReleaseRef,
        config: LoadBalancerConfig,
        observation: _ClusterObservation,
    ) -> LoadBalancerPlan:
        """Describe the strongest applicable evidence without promising allocation."""
        if observation.allocated_service is not None:
            service = observation.allocated_service
            return LoadBalancerPlan(
                release,
                config,
                "Reuse",
                f"default LoadBalancer implementation observed through {service.namespace}/{service.display_name}; allocation is confirmed per Service",
            )
        if observation.configured_metallb_namespaces:
            namespaces = ", ".join(observation.configured_metallb_namespaces)
            return LoadBalancerPlan(
                release,
                config,
                "Reuse",
                f"configured MetalLB observed in {namespaces}; pool capacity is confirmed per Service",
            )
        if observation.k3s_service_lb == "enabled":
            return LoadBalancerPlan(
                release,
                config,
                "Reuse",
                "k3s ServiceLB is enabled; host-port availability is confirmed per Service",
            )
        if observation.cloud_provider_metadata:
            return LoadBalancerPlan(
                release,
                config,
                "Reuse",
                "cloud provider metadata is present; external address allocation is confirmed per Service",
            )

        details: list[str] = []
        if observation.k3s_service_lb == "disabled":
            details.append("k3s ServiceLB is disabled")
        elif observation.k3s_service_lb == "unknown":
            details.append("k3s ServiceLB state could not be confirmed")
        if observation.metallb_api:
            details.append("MetalLB is installed without a configured address pool")
        if not details:
            details.append("no default LoadBalancer implementation could be confirmed")
        return LoadBalancerPlan(release, config, "Not verified", "; ".join(details))

    def _observe_cluster(self) -> _ClusterObservation:
        """Collect implementation evidence without creating probe Services."""
        kubectl = self._kubectl
        services = tuple(
            service
            for service in kubectl.list_all_resources(("services",))
            if (service.get("spec") or {}).get("type") == "LoadBalancer"
            and not (service.get("spec") or {}).get("loadBalancerClass")
        )
        allocated = next(
            (
                resource_ref(service)
                for service in services
                if ((service.get("status") or {}).get("loadBalancer") or {}).get(
                    "ingress"
                )
            ),
            None,
        )

        nodes = kubectl.list_cluster_resources(("nodes",))
        cloud_provider_metadata = any(
            _is_cloud_provider_id(str((node.get("spec") or {}).get("providerID") or ""))
            for node in nodes
        )

        supported = set(kubectl.api_resource_names(_METALLB_API_GROUP))
        selected = tuple(
            kind.resource
            for kind in (_ADDRESS_POOL, _L2_ADVERTISEMENT, _BGP_ADVERTISEMENT)
            if kind.resource in supported
        )
        values = kubectl.list_all_resources(selected) if selected else ()
        pool_namespaces = {
            resource_ref(value).namespace
            for value in values
            if value.get("kind") == _ADDRESS_POOL.kind
            and bool((value.get("spec") or {}).get("addresses"))
        }
        advertisement_namespaces = {
            resource_ref(value).namespace
            for value in values
            if value.get("kind") in {_L2_ADVERTISEMENT.kind, _BGP_ADVERTISEMENT.kind}
        }
        return _ClusterObservation(
            tuple(resource_ref(service) for service in services),
            allocated,
            bool(supported),
            tuple(resource_ref(value) for value in values),
            tuple(sorted(pool_namespaces & advertisement_namespaces)),
            _k3s_service_lb_state(nodes),
            cloud_provider_metadata,
        )

    def _managed_objects(self, release: ReleaseRef) -> tuple[dict[str, Any], ...]:
        """Return the live pool and advertisement named after the managed release."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        objects = []
        for kind in _MANAGED_KINDS:
            if kind.resource not in supported:
                continue
            value = self._kubectl.get_if_exists(
                kind.resource, release.name, release.namespace
            )
            if value is not None:
                objects.append(value)
        return tuple(objects)

    def _managed_resource_collision(self, release: ReleaseRef) -> str:
        """Return a conflict when a managed resource name is externally owned."""
        for value in self._managed_objects(release):
            if not _has_label(value, self._helm.management_label):
                return (
                    f"MetalLB {release.namespace}/{value.get('kind')}/{release.name} "
                    "already exists without Foretoken ownership; rename or manage it "
                    "through its current lifecycle"
                )
        return ""


def _managed_install_conflict(observation: _ClusterObservation) -> str:
    """Refuse a second default implementation that could claim shared Services."""
    if observation.metallb_api or observation.metallb_resources:
        return (
            "MetalLB is already installed; reuse its lifecycle instead of "
            "installing CLI-managed MetalLB"
        )
    if observation.k3s_service_lb == "enabled":
        return (
            "k3s ServiceLB is enabled; disable it on every server before "
            "installing CLI-managed MetalLB"
        )
    if observation.k3s_service_lb == "unknown":
        return (
            "k3s ServiceLB state could not be confirmed; disable it on every "
            "server before installing CLI-managed MetalLB"
        )
    if observation.allocated_service is not None:
        return (
            "a default LoadBalancer implementation already publishes addresses; "
            "reuse its existing lifecycle"
        )
    if observation.cloud_provider_metadata:
        return (
            "cloud provider metadata indicates a cluster-managed integration; "
            "confirm it is disabled before installing CLI-managed MetalLB"
        )
    return ""


def _external_metallb_resources(
    resources: tuple[ResourceRef, ...], release: ReleaseRef
) -> tuple[ResourceRef, ...]:
    """Return MetalLB configuration other than the two managed resources."""
    owned = {(kind.kind, release.name, release.namespace) for kind in _MANAGED_KINDS}
    return tuple(
        resource
        for resource in resources
        if (resource.kind, resource.name, resource.namespace) not in owned
    )


def _managed_configuration(
    release: ReleaseRef,
    management_label: tuple[str, str],
    addresses: tuple[str, ...],
) -> str:
    """Render the pool and Layer 2 advertisement applied and deleted by the CLI."""
    labels = {management_label[0]: management_label[1]}
    pool: dict[str, Any] = {
        "apiVersion": _METALLB_API_VERSION,
        "kind": _ADDRESS_POOL.kind,
        "metadata": {
            "name": release.name,
            "namespace": release.namespace,
            "labels": labels,
        },
    }
    advertisement: dict[str, Any] = {
        "apiVersion": _METALLB_API_VERSION,
        "kind": _L2_ADVERTISEMENT.kind,
        "metadata": {
            "name": release.name,
            "namespace": release.namespace,
            "labels": labels,
        },
    }
    if addresses:
        pool["spec"] = {"addresses": list(addresses)}
        advertisement["spec"] = {"ipAddressPools": [release.name]}
    return yaml.safe_dump_all(
        (pool, advertisement),
        explicit_start=True,
        sort_keys=False,
    )


def _has_label(value: dict[str, Any], label: tuple[str, str]) -> bool:
    """Return whether one Kubernetes object carries the exact ownership label."""
    labels = (value.get("metadata") or {}).get("labels") or {}
    return isinstance(labels, dict) and labels.get(label[0]) == label[1]


def _k3s_service_lb_state(nodes: tuple[dict[str, Any], ...]) -> str:
    """Infer the effective k3s ServiceLB flag from server node arguments."""
    k3s_nodes = tuple(
        node
        for node in nodes
        if "k3s" in str((node.get("status") or {}).get("nodeInfo", {}).get("kubeletVersion") or "")
    )
    if not k3s_nodes:
        return "not-k3s"
    server_arguments: list[tuple[str, ...]] = []
    for node in k3s_nodes:
        raw = str((node.get("metadata") or {}).get("annotations", {}).get("k3s.io/node-args") or "")
        if not raw:
            continue
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            return "unknown"
        if (
            isinstance(arguments, list)
            and all(isinstance(argument, str) for argument in arguments)
            and "server" in arguments
        ):
            server_arguments.append(tuple(arguments))
    if not server_arguments:
        return "unknown"
    disabled = tuple(_disabled_k3s_components(arguments) for arguments in server_arguments)
    if any("servicelb" in components for components in disabled):
        return "disabled"
    return "enabled"


def _disabled_k3s_components(arguments: tuple[str, ...]) -> set[str]:
    """Return components named by either supported k3s disable flag syntax."""
    disabled: set[str] = set()
    for index, argument in enumerate(arguments):
        if argument.startswith("--disable="):
            disabled.update(argument.partition("=")[2].split(","))
        elif argument == "--disable" and index + 1 < len(arguments):
            disabled.update(arguments[index + 1].split(","))
    return disabled


def _is_cloud_provider_id(provider_id: str) -> bool:
    """Return whether node metadata names a non-local infrastructure provider."""
    scheme, separator, _ = provider_id.partition("://")
    return bool(separator and scheme not in {"", "k3s", "kind", "minikube", "docker"})
