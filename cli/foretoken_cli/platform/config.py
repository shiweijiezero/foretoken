# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Fixed identities for CLI-managed platform releases and dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from foretoken_cli import package_version
from foretoken_cli.manifest import DeploymentError


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
    management_label: tuple[str, str]
    install_source_label: str
    platform: ManagedChart
    prometheus: ManagedChart
    dcgm_exporter: ManagedChart
    envoy_gateway: ManagedChart
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
        management_label=("foretoken.io/managed-by", "foretoken"),
        install_source_label="foretoken.io/install-source",
        platform=ManagedChart(
            release_name="foretoken",
            source="oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken",
            version=package_version(),
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


def validate_platform_values(paths: tuple[str, ...]) -> None:
    """Keep frontend topology under the CLI argument contract."""
    for path_value in paths:
        path = Path(path_value)
        try:
            values = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise DeploymentError(
                f"cannot read Helm values file {path}: {exc}"
            ) from exc
        if not isinstance(values, dict):
            continue
        frontend = values.get("frontend")
        if not isinstance(frontend, dict):
            continue
        reserved = tuple(key for key in ("mode", "gateway") if key in frontend)
        if reserved:
            names = ", ".join(f"frontend.{key}" for key in reserved)
            raise DeploymentError(
                f"Helm values file {path} sets {names}; use --frontend-mode "
                "and --gateway-* options for frontend topology"
            )
