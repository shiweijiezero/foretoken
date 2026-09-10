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

_METALLB_API_GROUP = "metallb.io"
_METALLB_POOL_RESOURCE = "ipaddresspools.metallb.io"
_METALLB_L2_RESOURCE = "l2advertisements.metallb.io"
_METALLB_BGP_RESOURCE = "bgpadvertisements.metallb.io"
_METALLB_CRDS = (
    "ipaddresspools.metallb.io",
    "l2advertisements.metallb.io",
)
_MANAGED_POOL = "foretoken"
_MANAGED_ADVERTISEMENT = "foretoken"


@dataclass(frozen=True)
class LoadBalancerPlan:
    """One resolved cluster LoadBalancer lifecycle decision."""

    release: ReleaseRef
    config: LoadBalancerConfig
    action: str
    detail: str
    install: bool = False
    blocking_reason: str = ""


@dataclass(frozen=True)
class _ClusterObservation:
    """Read-only evidence about implementations that may serve default Services."""

    load_balancer_services: tuple[dict[str, Any], ...]
    allocated_default_service: ResourceRef | None
    classed_service_count: int
    configured_metallb_namespaces: tuple[str, ...]
    ready_metallb_namespaces: tuple[str, ...]
    metallb_workload_namespaces: tuple[str, ...]
    metallb_resources: tuple[ResourceRef, ...]
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
        if release_exists and not helm.is_cli_managed(release):
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration",
                f"Helm release {release.display_name} is not managed by foretoken",
                blocking_reason=(
                    f"Helm release {release.display_name} is not managed by foretoken; "
                    "use its existing Helm lifecycle"
                ),
            )

        try:
            observation = self._observe_cluster()
            managed_configured = self._managed_configuration_is_owned(release)
        except DeploymentError as exc:
            detail = f"cluster capability could not be inspected: {exc}"
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration" if config.managed_addresses else "Not verified",
                detail,
                blocking_reason=detail if config.managed_addresses else "",
            )

        # A managed release stores its pool, so an interrupted or partially
        # upgraded installation resumes without asking for the addresses again.
        if release_exists and not config.managed_addresses:
            config = self._helm.metallb_load_balancer_config(release)
            if not config.managed_addresses:
                config = self._managed_resource_config(release)

        if not config.managed_addresses:
            if release_exists and managed_configured:
                return LoadBalancerPlan(
                    release,
                    config,
                    "Reuse",
                    f"CLI-managed MetalLB {release.display_name}; allocation is confirmed per Service",
                )
            return self._automatic_plan(release, config, observation, release_exists)

        collision = self._managed_resource_collision(release)
        if collision:
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration",
                collision,
                blocking_reason=collision,
            )
        if not release_exists:
            conflict = self._managed_install_conflict(observation)
            if conflict:
                return LoadBalancerPlan(
                    release,
                    config,
                    "Needs configuration",
                    conflict,
                    blocking_reason=conflict,
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
        self._kubectl.wait_for_crds(_METALLB_CRDS, timeout)
        self._kubectl.apply(
            _managed_configuration(
                plan.release.namespace,
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
            external_config = self._external_metallb_resources(
                observation.metallb_resources,
                release,
            )
            if external_config:
                names = ", ".join(resource.display_name for resource in external_config)
                return "Preserve", f"external MetalLB configuration remains: {names}"
            dependent_services = tuple(
                resource_ref(service)
                for service in observation.load_balancer_services
                if not (service.get("spec") or {}).get("loadBalancerClass")
            )
            if dependent_services:
                names = ", ".join(
                    f"{resource.namespace}/{resource.display_name}"
                    for resource in dependent_services
                )
                return "Preserve", f"default LoadBalancer Services remain: {names}"
            collision = self._managed_resource_collision(release)
            if collision:
                return "Preserve", collision
        except DeploymentError as exc:
            return "Preserve", f"dependency check failed: {exc}"

        if self._managed_configuration_exists(release):
            self._kubectl.delete(
                _managed_configuration(
                    release.namespace,
                    helm.management_label,
                    (),
                ),
                timeout,
            )
        helm.uninstall(release, timeout)
        return "Removed", release.display_name

    def _automatic_plan(
        self,
        release: ReleaseRef,
        config: LoadBalancerConfig,
        observation: _ClusterObservation,
        release_exists: bool,
    ) -> LoadBalancerPlan:
        """Describe the strongest applicable evidence without promising allocation."""
        if release_exists:
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration",
                "CLI-managed MetalLB exists without a recoverable Foretoken address pool; provide loadBalancer.managedAddresses",
            )
        if observation.ready_metallb_namespaces and (
            observation.k3s_service_lb == "enabled"
            or observation.cloud_provider_metadata
        ):
            return LoadBalancerPlan(
                release,
                config,
                "Needs configuration",
                "default-class MetalLB and another possible default implementation were observed; ask the cluster administrator to select one before deploying external Services",
            )
        if observation.allocated_default_service is not None:
            service = observation.allocated_default_service
            return LoadBalancerPlan(
                release,
                config,
                "Reuse",
                f"default LoadBalancer implementation observed through {service.namespace}/{service.display_name}; allocation is confirmed per Service",
            )
        if observation.ready_metallb_namespaces:
            namespaces = ", ".join(observation.ready_metallb_namespaces)
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
        if observation.configured_metallb_namespaces:
            details.append(
                "MetalLB configuration exists but default-class controller and speaker readiness was not confirmed"
            )
        elif observation.metallb_workload_namespaces:
            details.append("MetalLB workloads exist without an applicable address pool")
        if observation.classed_service_count:
            details.append(
                "only class-specific LoadBalancer Services were observed while Foretoken uses the default class"
            )
        if not details:
            details.append("no default LoadBalancer implementation could be confirmed")
        return LoadBalancerPlan(release, config, "Not verified", "; ".join(details))

    def _managed_install_conflict(
        self, observation: _ClusterObservation
    ) -> str:
        """Refuse a second default implementation that could claim shared Services."""
        if observation.metallb_resources or observation.metallb_workload_namespaces:
            return (
                "existing MetalLB workloads or configuration were observed; reuse "
                "their lifecycle instead of installing CLI-managed MetalLB"
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
        if observation.allocated_default_service is not None:
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

    def _observe_cluster(self) -> _ClusterObservation:
        """Collect implementation evidence without creating probe Services."""
        kubectl = self._kubectl
        services = tuple(
            service
            for service in kubectl.list_all_resources(("services",))
            if (service.get("spec") or {}).get("type") == "LoadBalancer"
        )
        allocated_default = next(
            (
                resource_ref(service)
                for service in services
                if not (service.get("spec") or {}).get("loadBalancerClass")
                and bool(
                    ((service.get("status") or {}).get("loadBalancer") or {}).get(
                        "ingress"
                    )
                )
            ),
            None,
        )
        classed_service_count = sum(
            bool((service.get("spec") or {}).get("loadBalancerClass"))
            for service in services
        )

        nodes = kubectl.list_cluster_resources(("nodes",))
        k3s_service_lb = _k3s_service_lb_state(nodes)
        cloud_provider_metadata = any(
            _is_cloud_provider_id(str((node.get("spec") or {}).get("providerID") or ""))
            for node in nodes
        )

        metallb_resources, configured_namespaces = self._metallb_configuration()
        workload_namespaces, default_class_namespaces = (
            self._metallb_workload_namespaces()
        )
        return _ClusterObservation(
            services,
            allocated_default,
            classed_service_count,
            configured_namespaces,
            tuple(
                sorted(
                    set(configured_namespaces) & set(default_class_namespaces)
                )
            ),
            workload_namespaces,
            metallb_resources,
            k3s_service_lb,
            cloud_provider_metadata,
        )

    def _metallb_configuration(
        self,
    ) -> tuple[tuple[ResourceRef, ...], tuple[str, ...]]:
        """Return existing MetalLB config and namespaces with pools plus advertisements."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        selected = tuple(
            name
            for name in (
                _METALLB_POOL_RESOURCE,
                _METALLB_L2_RESOURCE,
                _METALLB_BGP_RESOURCE,
            )
            if name in supported
        )
        if not selected:
            return (), ()
        values = self._kubectl.list_all_resources(selected)
        resources = tuple(resource_ref(value) for value in values)
        pool_namespaces = {
            resource_ref(value).namespace
            for value in values
            if value.get("kind") == "IPAddressPool"
            and bool((value.get("spec") or {}).get("addresses"))
        }
        advertisement_namespaces = {
            resource_ref(value).namespace
            for value in values
            if value.get("kind") in {"L2Advertisement", "BGPAdvertisement"}
        }
        return resources, tuple(sorted(pool_namespaces & advertisement_namespaces))

    def _metallb_workload_namespaces(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ready MetalLB namespaces for all and default-class workloads."""
        selector = "app.kubernetes.io/name=metallb"
        deployments = self._kubectl.list_all_resources(
            ("deployment.apps",), label_selector=selector
        )
        daemonsets = self._kubectl.list_all_resources(
            ("daemonset.apps",), label_selector=selector
        )
        controllers = {
            resource_ref(value).namespace
            for value in deployments
            if (value.get("metadata") or {}).get("labels", {}).get(
                "app.kubernetes.io/component"
            )
            == "controller"
            and _deployment_ready(value)
        }
        speakers = {
            resource_ref(value).namespace
            for value in daemonsets
            if (value.get("metadata") or {}).get("labels", {}).get(
                "app.kubernetes.io/component"
            )
            == "speaker"
            and _daemonset_ready(value)
        }
        ready = controllers & speakers
        default_controllers = {
            resource_ref(value).namespace
            for value in deployments
            if resource_ref(value).namespace in ready
            and _uses_default_load_balancer_class(value)
        }
        default_speakers = {
            resource_ref(value).namespace
            for value in daemonsets
            if resource_ref(value).namespace in ready
            and _uses_default_load_balancer_class(value)
        }
        return tuple(sorted(ready)), tuple(
            sorted(default_controllers & default_speakers)
        )

    def _managed_resource_config(self, release: ReleaseRef) -> LoadBalancerConfig:
        """Recover an address pool from Foretoken's fixed MetalLB resource."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        if _METALLB_POOL_RESOURCE not in supported:
            return LoadBalancerConfig()
        pool = self._kubectl.get_if_exists(
            _METALLB_POOL_RESOURCE, _MANAGED_POOL, release.namespace
        )
        if pool is None or not _has_label(pool, self._helm.management_label):
            return LoadBalancerConfig()
        addresses = (pool.get("spec") or {}).get("addresses") or []
        if not isinstance(addresses, list) or not all(
            isinstance(address, str) and address for address in addresses
        ):
            raise DeploymentError("managed MetalLB IPAddressPool is invalid")
        return LoadBalancerConfig(tuple(addresses))

    def _managed_configuration_is_owned(self, release: ReleaseRef) -> bool:
        """Return whether both fixed MetalLB resources carry Foretoken ownership."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        if not {_METALLB_POOL_RESOURCE, _METALLB_L2_RESOURCE}.issubset(supported):
            return False
        pool = self._kubectl.get_if_exists(
            _METALLB_POOL_RESOURCE, _MANAGED_POOL, release.namespace
        )
        advertisement = self._kubectl.get_if_exists(
            _METALLB_L2_RESOURCE, _MANAGED_ADVERTISEMENT, release.namespace
        )
        return pool is not None and advertisement is not None and all(
            _has_label(value, self._helm.management_label)
            for value in (pool, advertisement)
        )

    def _managed_configuration_exists(self, release: ReleaseRef) -> bool:
        """Return whether any fixed MetalLB resource remains under CLI ownership."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        for kind, name in (
            (_METALLB_POOL_RESOURCE, _MANAGED_POOL),
            (_METALLB_L2_RESOURCE, _MANAGED_ADVERTISEMENT),
        ):
            if kind not in supported:
                continue
            value = self._kubectl.get_if_exists(kind, name, release.namespace)
            if value is not None and _has_label(value, self._helm.management_label):
                return True
        return False

    def _managed_resource_collision(self, release: ReleaseRef) -> str:
        """Return a conflict when a fixed resource name is externally owned."""
        supported = set(self._kubectl.api_resource_names(_METALLB_API_GROUP))
        for kind, name in (
            (_METALLB_POOL_RESOURCE, _MANAGED_POOL),
            (_METALLB_L2_RESOURCE, _MANAGED_ADVERTISEMENT),
        ):
            if kind not in supported:
                continue
            value = self._kubectl.get_if_exists(kind, name, release.namespace)
            if value is not None and not _has_label(value, self._helm.management_label):
                return (
                    f"MetalLB {release.namespace}/{name} already exists without "
                    "Foretoken ownership; rename or manage it through its current lifecycle"
                )
        return ""

    def _external_metallb_resources(
        self,
        resources: tuple[ResourceRef, ...],
        release: ReleaseRef,
    ) -> tuple[ResourceRef, ...]:
        """Return MetalLB configuration outside Foretoken's two fixed resources."""
        owned = {
            ("IPAddressPool", _MANAGED_POOL, release.namespace),
            ("L2Advertisement", _MANAGED_ADVERTISEMENT, release.namespace),
        }
        return tuple(
            resource
            for resource in resources
            if (resource.kind, resource.name, resource.namespace) not in owned
        )


def _managed_configuration(
    namespace: str,
    management_label: tuple[str, str],
    addresses: tuple[str, ...],
) -> str:
    """Render the two fixed Layer 2 resources applied and deleted by the CLI."""
    labels = {management_label[0]: management_label[1]}
    pool: dict[str, Any] = {
        "apiVersion": "metallb.io/v1beta1",
        "kind": "IPAddressPool",
        "metadata": {"name": _MANAGED_POOL, "namespace": namespace, "labels": labels},
    }
    if addresses:
        pool["spec"] = {"addresses": list(addresses)}
    advertisement: dict[str, Any] = {
        "apiVersion": "metallb.io/v1beta1",
        "kind": "L2Advertisement",
        "metadata": {
            "name": _MANAGED_ADVERTISEMENT,
            "namespace": namespace,
            "labels": labels,
        },
    }
    if addresses:
        advertisement["spec"] = {"ipAddressPools": [_MANAGED_POOL]}
    return yaml.safe_dump_all(
        (pool, advertisement),
        explicit_start=True,
        sort_keys=False,
    )


def _has_label(value: dict[str, Any], label: tuple[str, str]) -> bool:
    """Return whether one Kubernetes object carries the exact ownership label."""
    labels = (value.get("metadata") or {}).get("labels") or {}
    return isinstance(labels, dict) and labels.get(label[0]) == label[1]


def _deployment_ready(value: dict[str, Any]) -> bool:
    """Return whether a Deployment reports all desired replicas available."""
    desired = int((value.get("spec") or {}).get("replicas") or 1)
    available = int((value.get("status") or {}).get("availableReplicas") or 0)
    return available >= desired


def _daemonset_ready(value: dict[str, Any]) -> bool:
    """Return whether a DaemonSet reports every desired Pod ready."""
    status = value.get("status") or {}
    desired = int(status.get("desiredNumberScheduled") or 0)
    ready = int(status.get("numberReady") or 0)
    return desired > 0 and ready >= desired


def _uses_default_load_balancer_class(value: dict[str, Any]) -> bool:
    """Return whether a MetalLB workload handles unclassified Services."""
    containers = ((value.get("spec") or {}).get("template") or {}).get("spec", {}).get(
        "containers"
    ) or []
    return not any(
        isinstance(argument, str)
        and argument.startswith("--lb-class=")
        and argument.partition("=")[2]
        for container in containers
        if isinstance(container, dict)
        for argument in container.get("args") or []
    )


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
