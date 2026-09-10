# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Fixed identities for CLI-managed platform releases and dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken import platform_version
from foretoken.manifest import DeploymentError
from foretoken.platform.types import LoadBalancerConfig


@dataclass(frozen=True)
class ManagedChart:
    """Describe one Helm chart whose release lifecycle belongs to the CLI."""

    release_name: str
    source: str
    version: str | None = None
    repository: str | None = None


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
    envoy_gateway_default_controller: str
    envoy_gateway_controller: str
    dcgm_metrics: str

    @property
    def platform_selector_labels(self) -> tuple[tuple[str, str], ...]:
        """Return labels shared by resources in the platform Helm release."""
        return (
            ("app.kubernetes.io/name", "foretoken-control-plane"),
            ("app.kubernetes.io/instance", self.platform.release_name),
        )


def default_platform_config() -> PlatformConfig:
    """Return the version-aligned configuration owned by the installed CLI."""
    return PlatformConfig(
        namespace="foretoken-platform",
        load_balancer_namespace="metallb-system",
        management_label=("foretoken.io/managed-by", "foretoken"),
        legacy_management_label=("foretoken.io/managed-by", "foretoken-cli"),
        install_source_label="foretoken.io/install-source",
        platform=ManagedChart(
            release_name="foretoken",
            source="oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken",
            version=platform_version(),
        ),
        prometheus=ManagedChart(
            release_name="foretoken-prometheus",
            source="oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack",
            version="88.5.2",
        ),
        dcgm_exporter=ManagedChart(
            release_name="foretoken-dcgm-exporter",
            source=(
                "https://nvidia.github.io/dcgm-exporter/helm-charts/"
                "dcgm-exporter-4.8.3.tgz"
            ),
        ),
        envoy_gateway=ManagedChart(
            release_name="foretoken-envoy-gateway",
            source="oci://docker.io/envoyproxy/gateway-helm",
            version="v1.9.1",
        ),
        metallb=ManagedChart(
            release_name="foretoken-metallb",
            source="metallb",
            version="0.16.1",
            repository="https://metallb.github.io/metallb",
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


def load_balancer_config_from_values(
    values: dict[str, Any],
    fallback: LoadBalancerConfig | None = None,
) -> LoadBalancerConfig:
    """Decode the CLI-owned LoadBalancer settings from effective Helm values."""
    config = fallback or LoadBalancerConfig()
    load_balancer = values.get("loadBalancer")
    if load_balancer is None:
        return config
    if not isinstance(load_balancer, dict):
        raise DeploymentError("loadBalancer must be a mapping")
    if "managedAddresses" not in load_balancer:
        return config
    addresses = load_balancer["managedAddresses"]
    if not isinstance(addresses, list) or not all(
        isinstance(address, str) and address.strip() for address in addresses
    ):
        raise DeploymentError(
            "loadBalancer.managedAddresses must be a list of IP ranges or CIDRs"
        )
    normalized = tuple(address.strip() for address in addresses)
    if len(set(normalized)) != len(normalized):
        raise DeploymentError(
            "loadBalancer.managedAddresses must not contain duplicate ranges"
        )
    return LoadBalancerConfig(normalized)


def resolve_load_balancer_config(
    values: tuple[dict[str, Any], ...],
    stored: LoadBalancerConfig | None = None,
) -> LoadBalancerConfig:
    """Apply values files in Helm order to the stored LoadBalancer choice."""
    config = stored or LoadBalancerConfig()
    for item in values:
        config = load_balancer_config_from_values(item, config)
    return config
