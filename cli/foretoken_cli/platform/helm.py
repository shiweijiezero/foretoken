# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Helm chart adapters for CLI-managed Foretoken platform releases."""

from __future__ import annotations

import json

from foretoken_cli.manifest import DeploymentError
from foretoken_cli.platform.helm_client import HelmClient
from foretoken_cli.platform.types import PlatformGatewayConfig, ReleaseRef
from foretoken_cli.source import SourceImages


class Helm(HelmClient):
    """Build and execute Helm operations for platform-owned charts."""

    def platform_release(self) -> ReleaseRef:
        """Return the single Foretoken platform release managed by the CLI."""
        return ReleaseRef(self._config.platform.release_name, self._config.namespace)

    def prometheus_release(self) -> ReleaseRef:
        """Return the Prometheus release managed with the platform."""
        return ReleaseRef(self._config.prometheus.release_name, self._config.namespace)

    def dcgm_release(self) -> ReleaseRef:
        """Return the NVIDIA exporter release managed with the platform."""
        return ReleaseRef(
            self._config.dcgm_exporter.release_name, self._config.namespace
        )

    def envoy_gateway_release(self) -> ReleaseRef:
        """Return the Envoy Gateway release managed with the platform."""
        return ReleaseRef(
            self._config.envoy_gateway.release_name, self._config.namespace
        )

    @property
    def platform_selector_labels(self) -> tuple[tuple[str, str], ...]:
        """Return labels shared by resources in the platform release."""
        return self._config.platform_selector_labels

    @property
    def envoy_gateway_default_controller(self) -> str:
        """Return the upstream Envoy Gateway controller identity."""
        return self._config.envoy_gateway_default_controller

    @property
    def envoy_gateway_controller(self) -> str:
        """Return the controller identity reserved for managed Envoy Gateway."""
        return self._config.envoy_gateway_controller

    def platform_gateway_config(self, release: ReleaseRef) -> PlatformGatewayConfig:
        """Return the effective frontend Gateway configuration for a platform."""
        frontend = self._release_values(release).get("frontend") or {}
        if not isinstance(frontend, dict):
            raise DeploymentError("platform Gateway values are invalid")
        gateway = frontend.get("gateway") or {}
        if not isinstance(gateway, dict):
            raise DeploymentError("platform Gateway values are invalid")
        return PlatformGatewayConfig(
            mode=str(frontend.get("mode") or "local"),
            create=bool(gateway.get("create")),
            controller_name=str(gateway.get("controllerName") or ""),
            name=str(gateway.get("name") or ""),
            namespace=str(gateway.get("namespace") or ""),
            section_name=str(gateway.get("sectionName") or ""),
        )

    def platform_image_references(
        self, release: ReleaseRef
    ) -> tuple[str, str, str]:
        """Return the image references currently stored for a source release."""
        values = self._release_values(release)
        image = values.get("image") or {}
        frontend = values.get("frontend") or {}
        runtime = values.get("runtime") or {}
        vllm = (runtime.get("vllm") or {}) if isinstance(runtime, dict) else {}
        if not all(isinstance(value, dict) for value in (image, frontend, vllm)):
            raise DeploymentError("source release image values are invalid")
        repository = image.get("repository")
        tag = image.get("tag")
        frontend_image = frontend.get("image")
        model_server_image = vllm.get("image")
        if not all(
            isinstance(value, str) and value
            for value in (repository, tag, frontend_image, model_server_image)
        ):
            raise DeploymentError("source release image values are incomplete")
        return (
            f"{repository}:{tag}",
            frontend_image,
            model_server_image,
        )

    def _upgrade_install_args(
        self,
        release: ReleaseRef,
        chart: str,
        chart_version: str | None,
        release_labels: tuple[tuple[str, str], ...] = (),
    ) -> list[str]:
        """Build the shared CLI-owned Helm release identity and chart selection."""
        labels = (self._config.management_label, *release_labels)
        args = [
            "upgrade",
            "--install",
            release.name,
            chart,
            "--namespace",
            release.namespace,
            "--create-namespace",
            "--labels",
            ",".join(f"{key}={value}" for key, value in labels),
        ]
        if chart_version is not None:
            args.extend(["--version", chart_version])
        return args

    @staticmethod
    def _finish_upgrade(args: list[str], timeout: str) -> None:
        """Wait for one managed Helm upgrade to finish."""
        args.extend(["--wait", f"--timeout={timeout}"])

    def _managed_chart_args(
        self,
        release: ReleaseRef,
        chart: str,
        chart_version: str | None,
        timeout: str,
    ) -> list[str]:
        """Build one managed chart upgrade and wait configuration."""
        args = self._upgrade_install_args(release, chart, chart_version)
        self._finish_upgrade(args, timeout)
        return args

    @staticmethod
    def _add_platform_values(
        args: list[str],
        values: tuple[str, ...],
        frontend_mode: str | None,
        gateway_name: str,
        gateway_namespace: str,
        gateway_section_name: str,
        gateway_controller_name: str,
        observability_labels: tuple[tuple[str, str], ...],
    ) -> None:
        """Add the platform values shared by release and source installs."""
        for values_file in values:
            args.extend(["--values", values_file])
        args.extend(
            [
                "--set",
                "frontend.enabled=true",
                "--set",
                "observability.mode=enabled",
            ]
        )
        if observability_labels:
            args.extend(
                [
                    "--set-json",
                    "observability.additionalLabels="
                    + json.dumps(dict(observability_labels), separators=(",", ":")),
                ]
            )
        if frontend_mode is not None:
            gateway_create = frontend_mode == "gateway" and not gateway_name
            args.extend(["--set", f"frontend.mode={frontend_mode}"])
            args.extend(
                [
                    "--set",
                    f"frontend.gateway.create={str(gateway_create).lower()}",
                ]
            )
        if gateway_controller_name:
            args.extend(
                [
                    "--set-string",
                    f"frontend.gateway.controllerName={gateway_controller_name}",
                ]
            )
        if gateway_name:
            args.extend(["--set-string", f"frontend.gateway.name={gateway_name}"])
        if gateway_namespace:
            args.extend(
                ["--set-string", f"frontend.gateway.namespace={gateway_namespace}"]
            )
        if gateway_section_name or (
            frontend_mode == "gateway" and gateway_name
        ):
            args.extend(
                [
                    "--set-string",
                    f"frontend.gateway.sectionName={gateway_section_name}",
                ]
            )

    def install_platform(
        self,
        *,
        release: ReleaseRef,
        source_images: SourceImages | None,
        values: tuple[str, ...],
        frontend_mode: str | None,
        gateway_name: str,
        gateway_namespace: str,
        gateway_section_name: str,
        gateway_controller_name: str,
        observability_labels: tuple[tuple[str, str], ...],
        reuse_values: bool,
        timeout: str,
    ) -> None:
        """Install or update the CLI-owned Foretoken platform release."""
        source_mode = source_images is not None
        chart = (
            str(source_images.source_root / "deploy" / "charts" / "foretoken")
            if source_images is not None
            else self._config.platform.source
        )
        chart_version = None if source_mode else self._config.platform.version
        args = self._upgrade_install_args(
            release,
            chart,
            chart_version,
            (
                (
                    self._config.install_source_label,
                    "source" if source_mode else "release",
                ),
            ),
        )
        if reuse_values:
            args.append("--reuse-values")
        self._add_platform_values(
            args,
            values,
            frontend_mode,
            gateway_name,
            gateway_namespace,
            gateway_section_name,
            gateway_controller_name,
            observability_labels,
        )
        if source_images is not None:
            control_plane_image = source_images.control_plane
            frontend_image = source_images.frontend
            model_server_image = source_images.model_server
            if reuse_values and not all(
                (
                    source_images.control_plane_changed,
                    source_images.frontend_changed,
                    source_images.model_server_changed,
                )
            ):
                current_images = self.platform_image_references(release)
                if not source_images.control_plane_changed:
                    control_plane_image = current_images[0]
                if not source_images.frontend_changed:
                    frontend_image = current_images[1]
                if not source_images.model_server_changed:
                    model_server_image = current_images[2]
            repository, tag = _image_repository_tag(control_plane_image)
            args.extend(
                [
                    "--set-string",
                    f"image.repository={repository}",
                    "--set-string",
                    f"image.tag={tag}",
                    "--set-string",
                    "image.digest=",
                    "--set",
                    "image.pullPolicy="
                    + (
                        "Never"
                        if source_images.image_mode == "import"
                        else "IfNotPresent"
                    ),
                    "--set-string",
                    f"frontend.image={frontend_image}",
                    "--set-string",
                    f"runtime.vllm.image={model_server_image}",
                ]
            )
        self._finish_upgrade(args, timeout)
        self.run(args)

    def install_envoy_gateway(
        self,
        release: ReleaseRef,
        timeout: str,
    ) -> None:
        """Install or update the CLI-managed Envoy Gateway release."""
        args = self._managed_chart_args(
            release,
            self._config.envoy_gateway.source,
            self._config.envoy_gateway.version,
            timeout,
        )
        args.extend(
            [
                "--set-string",
                "config.envoyGateway.gateway.controllerName="
                + self._config.envoy_gateway_controller,
            ]
        )
        self.run(args)

    def install_prometheus(
        self,
        release: ReleaseRef,
        service_monitor_namespaces: tuple[str, ...],
        timeout: str,
    ) -> None:
        """Install or upgrade the CLI-managed kube-prometheus-stack release."""
        selected_namespaces = tuple(sorted(set(service_monitor_namespaces)))
        if not selected_namespaces:
            raise DeploymentError("managed Prometheus requires a monitor namespace")
        namespace_selector = (
            {
                "matchLabels": {
                    "kubernetes.io/metadata.name": selected_namespaces[0],
                }
            }
            if len(selected_namespaces) == 1
            else {
                "matchExpressions": [
                    {
                        "key": "kubernetes.io/metadata.name",
                        "operator": "In",
                        "values": list(selected_namespaces),
                    }
                ]
            }
        )
        rule_selector = {
            "matchLabels": {
                "app.kubernetes.io/name": "foretoken-control-plane",
            }
        }
        args = self._managed_chart_args(
            release,
            self._config.prometheus.source,
            self._config.prometheus.version,
            timeout,
        )
        args.extend(
            [
                "--set",
                "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false",
                "--set-json",
                "prometheus.prometheusSpec.serviceMonitorSelector={}",
                "--set-json",
                "prometheus.prometheusSpec.serviceMonitorNamespaceSelector="
                + json.dumps(namespace_selector, separators=(",", ":")),
                "--set",
                "prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false",
                "--set-json",
                "prometheus.prometheusSpec.ruleSelector="
                + json.dumps(rule_selector, separators=(",", ":")),
                "--set-json",
                "prometheus.prometheusSpec.ruleNamespaceSelector="
                + json.dumps(namespace_selector, separators=(",", ":")),
            ]
        )
        self.run(args)

    def install_dcgm_exporter(
        self,
        release: ReleaseRef,
        observability_labels: tuple[tuple[str, str], ...],
        node_selector: tuple[str, str] | None,
        reuse_values: bool,
        timeout: str,
    ) -> None:
        """Install or upgrade the CLI-managed NVIDIA DCGM Exporter release."""
        args = self._managed_chart_args(
            release,
            self._config.dcgm_exporter.source,
            None,
            timeout,
        )
        if reuse_values:
            args.append("--reuse-values")
        args.extend(
            [
                "--set",
                "serviceMonitor.enabled=true",
                "--set-string",
                "customMetrics=" + self._config.dcgm_metrics.replace(",", "\\,"),
                "--set-json",
                "securityContext.capabilities.add=[]",
            ]
        )
        if observability_labels:
            args.extend(
                [
                    "--set-json",
                    "serviceMonitor.additionalLabels="
                    + json.dumps(dict(observability_labels), separators=(",", ":")),
                ]
            )
        rendered_node_selector = (
            {} if node_selector is None else {node_selector[0]: node_selector[1]}
        )
        args.extend(
            [
                "--set-json",
                "nodeSelector="
                + json.dumps(rendered_node_selector, separators=(",", ":")),
            ]
        )
        self.run(args)


def _image_repository_tag(reference: str) -> tuple[str, str]:
    """Split one tagged image reference produced by the source workflow."""
    repository, separator, tag = reference.rpartition(":")
    if not separator or not repository or not tag:
        raise DeploymentError(f"source image must include a tag: {reference}")
    return repository, tag
