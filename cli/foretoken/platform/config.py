# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Fixed identities for CLI-managed platform releases and dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken import platform_version
from foretoken.manifest import DeploymentError
from foretoken.platform.types import LoadBalancerConfig, RuntimeOverrides


@dataclass(frozen=True)
class ManagedChart:
    """Describe one Helm chart whose release lifecycle belongs to the CLI."""

    release_name: str
    source: str
    version: str | None = None


@dataclass(frozen=True)
class PlatformConfig:
    """Define the internal identities shared by one CLI-managed platform."""

    namespace: str
    load_balancer_namespace: str
    management_label: tuple[str, str]
    legacy_management_label: tuple[str, str]
    install_source_label: str
    platform: ManagedChart
    prometheus: ManagedChart
    dcgm_exporter: ManagedChart
    envoy_gateway: ManagedChart
    metallb: ManagedChart
    leader_worker: ManagedChart
    envoy_gateway_default_controller: str
    envoy_gateway_controller: str
    dcgm_metrics: str
    metax_exporter_image: str | None
    image_registry: str | None

    @property
    def platform_selector_labels(self) -> tuple[tuple[str, str], ...]:
        """Return labels shared by resources in the platform Helm release."""
        return (
            ("app.kubernetes.io/name", "foretoken-control-plane"),
            ("app.kubernetes.io/instance", self.platform.release_name),
        )


def _oci_registry(value: str | None) -> str | None:
    """Normalize an explicit OCI mirror prefix without accepting embedded credentials."""
    registry = (value or "").strip().rstrip("/")
    if not registry:
        return None
    if "://" in registry or "@" in registry or registry.startswith("/"):
        raise DeploymentError(
            "OCI registry must be HOST[/PATH] without a URL scheme or credentials"
        )
    return registry


def _chart_source(
    registry: str | None,
    source: str,
    mirror_path: str | None = None,
) -> str:
    """Return the public chart source or its deterministic path in a user mirror."""
    if registry is None:
        return source
    path = mirror_path or source.removeprefix("oci://")
    return f"oci://{registry}/{path}"


def default_platform_config(oci_registry: str | None = None) -> PlatformConfig:
    """Return version-aligned release identities and optional OCI mirror paths."""
    registry = _oci_registry(oci_registry)
    return PlatformConfig(
        namespace="foretoken-platform",
        load_balancer_namespace="metallb-system",
        management_label=("foretoken.io/managed-by", "foretoken"),
        legacy_management_label=("foretoken.io/managed-by", "foretoken-cli"),
        install_source_label="foretoken.io/install-source",
        platform=ManagedChart(
            release_name="foretoken",
            source=_chart_source(
                registry,
                "oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken",
            ),
            version=platform_version(),
        ),
        prometheus=ManagedChart(
            release_name="foretoken-prometheus",
            source=_chart_source(
                registry,
                "oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack",
            ),
            version="88.5.2",
        ),
        dcgm_exporter=ManagedChart(
            release_name="foretoken-dcgm-exporter",
            source=_chart_source(
                registry,
                "https://nvidia.github.io/dcgm-exporter/helm-charts/"
                "dcgm-exporter-4.8.3.tgz",
                "nvidia.github.io/dcgm-exporter/helm-charts/dcgm-exporter",
            ),
            version="4.8.3" if registry else None,
        ),
        envoy_gateway=ManagedChart(
            release_name="foretoken-envoy-gateway",
            source=_chart_source(
                registry,
                "oci://docker.io/envoyproxy/gateway-helm",
            ),
            version="v1.9.1",
        ),
        metallb=ManagedChart(
            release_name="foretoken-metallb",
            source=_chart_source(
                registry,
                "oci://quay.io/metallb/chart/metallb",
            ),
            version="0.16.1",
        ),
        leader_worker=ManagedChart(
            release_name="foretoken-lws",
            source=_chart_source(registry, "oci://registry.k8s.io/lws/charts/lws"),
            version="0.10.0",
        ),
        envoy_gateway_default_controller=(
            "gateway.envoyproxy.io/gatewayclass-controller"
        ),
        envoy_gateway_controller=(
            "gateway.foretoken.io/gatewayclass-controller"
        ),
        dcgm_metrics="""# Foretoken hardware metrics
DCGM_FI_DEV_GPU_UTIL, gauge, GPU utilization (in %).
DCGM_FI_DEV_MEM_COPY_UTIL, gauge, Memory utilization (in %).
DCGM_FI_DEV_FB_FREE, gauge, Framebuffer memory free (in MiB).
DCGM_FI_DEV_FB_USED, gauge, Framebuffer memory used (in MiB).
DCGM_FI_DEV_POWER_USAGE, gauge, Power draw (in W).
DCGM_FI_DEV_GPU_TEMP, gauge, GPU temperature (in C).
DCGM_FI_DEV_XID_ERRORS, gauge, Last XID error code.
""",
        metax_exporter_image=os.environ.get("FORETOKEN_METAX_EXPORTER_IMAGE"),
        image_registry=registry,
    )


def load_platform_values(paths: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    """Read Helm values files and keep frontend topology under the CLI contract."""
    loaded: list[dict[str, Any]] = []
    for path_value in paths:
        path = Path(path_value)
        try:
            values = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise DeploymentError(
                f"cannot read Helm values file {path}: {exc}"
            ) from exc
        if not isinstance(values, dict):
            loaded.append({})
            continue
        global_values = values.get("global")
        if isinstance(global_values, dict) and "imageRegistry" in global_values:
            raise DeploymentError(
                f"Helm values file {path} sets global.imageRegistry; use --oci-registry"
            )
        observability = values.get("observability")
        if isinstance(observability, dict) and "prometheus" in observability:
            raise DeploymentError(
                f"Helm values file {path} sets observability.prometheus; use --prometheus"
            )
        frontend = values.get("frontend")
        if isinstance(frontend, dict):
            reserved = tuple(key for key in ("mode", "gateway") if key in frontend)
            if reserved:
                names = ", ".join(f"frontend.{key}" for key in reserved)
                raise DeploymentError(
                    f"Helm values file {path} sets {names}; use --frontend-mode "
                    "and --gateway-* options for frontend topology"
                )
        loaded.append(values)
    return tuple(loaded)


def runtime_overrides_from_values(
    values: tuple[dict[str, Any], ...],
) -> RuntimeOverrides:
    """Return runtime fields explicitly set by an ordered Helm values stack."""
    image: str | None = None
    resource_name: str | None = None
    selector_key: str | None = None
    selector_value: str | None = None
    for item in values:
        runtime = item.get("runtime")
        if not isinstance(runtime, dict):
            continue
        vllm = runtime.get("vllm")
        if not isinstance(vllm, dict):
            continue
        if "image" in vllm:
            value = vllm["image"]
            if not isinstance(value, str):
                raise DeploymentError("runtime.vllm.image must be a string")
            image = value
        gpu = vllm.get("gpu")
        if not isinstance(gpu, dict):
            continue
        if "resourceName" in gpu:
            value = gpu["resourceName"]
            if not isinstance(value, str):
                raise DeploymentError(
                    "runtime.vllm.gpu.resourceName must be a string"
                )
            resource_name = value
        selector = gpu.get("nodeSelector")
        if not isinstance(selector, dict):
            continue
        if "key" in selector:
            value = selector["key"]
            if not isinstance(value, str):
                raise DeploymentError(
                    "runtime.vllm.gpu.nodeSelector.key must be a string"
                )
            selector_key = value
        if "value" in selector:
            value = selector["value"]
            if not isinstance(value, str):
                raise DeploymentError(
                    "runtime.vllm.gpu.nodeSelector.value must be a string"
                )
            selector_value = value

    if bool(selector_key) != bool(selector_value):
        raise DeploymentError(
            "runtime.vllm.gpu.nodeSelector.key and value must be set together"
        )
    selector = (
        (selector_key, selector_value)
        if selector_key is not None and selector_value is not None
        else None
    )
    return RuntimeOverrides(image, resource_name, selector)


def grafana_anonymous_access_from_values(
    values: tuple[dict[str, Any], ...],
) -> bool | None:
    """Read the last explicit Grafana access choice for the managed monitoring release."""
    anonymous_access = None
    for item in values:
        observability = item.get("observability", {})
        if not isinstance(observability, dict):
            raise DeploymentError("observability must be a mapping")
        grafana = observability.get("grafana", {})
        if not isinstance(grafana, dict):
            raise DeploymentError("observability.grafana must be a mapping")
        if "anonymousAccess" in grafana:
            anonymous_access = grafana["anonymousAccess"]
            if not isinstance(anonymous_access, bool):
                raise DeploymentError("observability.grafana.anonymousAccess must be a boolean")
    return anonymous_access


def load_balancer_config_from_values(
    values: dict[str, Any],
) -> LoadBalancerConfig | None:
    """Decode loadBalancer.managedAddresses, or None when the values leave it unset."""
    load_balancer = values.get("loadBalancer")
    if load_balancer is None:
        return None
    if not isinstance(load_balancer, dict):
        raise DeploymentError("loadBalancer must be a mapping")
    if "managedAddresses" not in load_balancer:
        return None
    addresses = load_balancer["managedAddresses"]
    if not isinstance(addresses, list) or not all(
        isinstance(address, str) and address for address in addresses
    ):
        raise DeploymentError(
            "loadBalancer.managedAddresses must be a list of IP ranges or CIDRs"
        )
    return LoadBalancerConfig(tuple(addresses))


def resolve_load_balancer_config(
    values: tuple[dict[str, Any], ...],
) -> LoadBalancerConfig:
    """Return the LoadBalancer choice from the last values file that sets it."""
    config = LoadBalancerConfig()
    for item in values:
        decoded = load_balancer_config_from_values(item)
        if decoded is not None:
            config = decoded
    return config
