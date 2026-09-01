# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Helm lifecycle for CLI-managed Foretoken platform releases."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, NoReturn

from foretoken_cli.manifest import DeploymentError
from foretoken_cli.source import SourceImages

@dataclass(frozen=True)
class ReleaseRef:
    """A named Helm release whose lifecycle may be owned by the CLI."""

    name: str
    namespace: str

    @property
    def display_name(self) -> str:
        """Return the stable release identity shown in install plans."""
        return f"{self.namespace}/{self.name}"

    @property
    def management_label(self) -> tuple[str, str]:
        """Return the Helm label that records CLI lifecycle ownership."""
        return "foretoken.io/managed-by", "foretoken-cli"


def platform_release() -> ReleaseRef:
    """Return the fixed CLI-managed Foretoken platform release."""
    return ReleaseRef("foretoken", "foretoken-platform")


def prometheus_release() -> ReleaseRef:
    """Return the fixed CLI-managed Prometheus release."""
    return ReleaseRef("foretoken-prometheus", platform_release().namespace)


def dcgm_release() -> ReleaseRef:
    """Return the fixed CLI-managed NVIDIA exporter release."""
    return ReleaseRef("foretoken-dcgm-exporter", platform_release().namespace)


class Helm:
    """Read and mutate Helm releases through the local Helm CLI."""

    def __init__(self) -> None:
        if shutil.which("helm") is None:
            raise DeploymentError("helm is required to install the Foretoken platform")

    def run(self, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
        """Execute Helm and preserve its diagnostic output on failure."""
        command = ["helm", *args]
        completed = self._execute(command)
        if completed.returncode:
            self._raise_command_error(command, completed)
        return completed

    @staticmethod
    def _execute(command: list[str]) -> subprocess.CompletedProcess[str]:
        """Execute one fully assembled Helm command without interpreting failure."""
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _raise_command_error(
        command: list[str], completed: subprocess.CompletedProcess[str]
    ) -> NoReturn:
        """Raise the shared user-facing Helm failure."""
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeploymentError(f"{shlex.join(command)} failed: {detail}")

    def release_exists(self, release: ReleaseRef) -> bool:
        """Return whether a release with the fixed identity exists."""
        command = [
            "helm",
            "get",
            "metadata",
            release.name,
            "--namespace",
            release.namespace,
            "--output",
            "json",
        ]
        completed = self._execute(command)
        if completed.returncode == 0:
            return True
        detail = completed.stderr.strip() or completed.stdout.strip()
        if "release: not found" in detail:
            return False
        self._raise_command_error(command, completed)

    def is_cli_managed(self, release: ReleaseRef) -> bool:
        """Return whether Helm storage assigns the release to this CLI."""
        key, value = release.management_label
        return bool(self._list_releases(release, selector=f"{key}={value}"))

    def has_release_label(
        self, release: ReleaseRef, key: str, value: str
    ) -> bool:
        """Return whether Helm storage carries one exact release label."""
        return bool(
            self._list_releases(release, selector=f"{key}={value}")
        )

    def platform_image_references(
        self, release: ReleaseRef
    ) -> tuple[str, str, str]:
        """Return the image references currently stored for a source release."""
        values = _decode_json(
            self.run(
                [
                    "get",
                    "values",
                    release.name,
                    "--namespace",
                    release.namespace,
                    "--all",
                    "--output",
                    "json",
                ]
            ).stdout
        )
        if not isinstance(values, dict):
            raise DeploymentError("helm get values returned an unexpected JSON value")
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

    def release_install_source(self, release: ReleaseRef) -> str:
        """Return the recorded platform installation source, when present."""
        for source in ("release", "source"):
            if self.has_release_label(release, "foretoken.io/install-source", source):
                return source
        return ""

    def _list_releases(
        self, release: ReleaseRef, *, selector: str = ""
    ) -> tuple[dict[str, Any], ...]:
        """List one fixed release, optionally filtered by Helm metadata labels."""
        args = [
            "list",
            "--all",
            "--namespace",
            release.namespace,
            "--filter",
            f"^{re.escape(release.name)}$",
            "--output",
            "json",
            "--max",
            "1",
        ]
        if selector:
            args.extend(["--selector", selector])
        listed = _decode_json(self.run(args).stdout)
        if not isinstance(listed, list) or not all(
            isinstance(item, dict) for item in listed
        ):
            raise DeploymentError("helm list returned an unexpected JSON value")
        return tuple(listed)

    @staticmethod
    def _upgrade_install_args(
        release: ReleaseRef,
        chart: str,
        chart_version: str | None,
        release_labels: tuple[tuple[str, str], ...] = (),
    ) -> list[str]:
        """Build the shared CLI-owned Helm release identity and chart selection."""
        labels = (release.management_label, *release_labels)
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
    def _finish_upgrade(args: list[str], timeout: str, dry_run: bool) -> None:
        """Add the execution mode shared by managed Helm upgrades."""
        if dry_run:
            args.extend(["--dry-run=server", "--hide-secret"])
        else:
            args.extend(["--wait", f"--timeout={timeout}"])

    @staticmethod
    def _add_platform_values(
        args: list[str],
        values: tuple[str, ...],
        frontend_mode: str | None,
        gateway_name: str,
        gateway_namespace: str,
        gateway_section_name: str,
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
        if gateway_name:
            args.extend(["--set-string", f"frontend.gateway.name={gateway_name}"])
        if gateway_namespace:
            args.extend(
                ["--set-string", f"frontend.gateway.namespace={gateway_namespace}"]
            )
        if gateway_section_name:
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
        observability_labels: tuple[tuple[str, str], ...],
        reuse_values: bool,
        timeout: str,
        dry_run: bool,
        dry_run_api_versions: tuple[str, ...] = (),
    ) -> None:
        """Install or update the CLI-owned Foretoken platform release."""
        source_mode = source_images is not None
        chart = (
            str(source_images.source_root / "deploy" / "charts" / "foretoken")
            if source_images is not None
            else "oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken"
        )
        chart_version = None if source_mode else version("foretoken-cli")
        render_template = dry_run and bool(dry_run_api_versions)
        if render_template:
            args = ["template", release.name, chart, "--namespace", release.namespace]
            if chart_version is not None:
                args.extend(["--version", chart_version])
        else:
            args = self._upgrade_install_args(
                release,
                chart,
                chart_version,
                (("foretoken.io/install-source", "source" if source_mode else "release"),),
            )
        if reuse_values and not render_template:
            args.append("--reuse-values")
        self._add_platform_values(
            args,
            values,
            frontend_mode,
            gateway_name,
            gateway_namespace,
            gateway_section_name,
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
        if render_template:
            for api_version in dry_run_api_versions:
                args.extend(["--api-versions", api_version])
        else:
            self._finish_upgrade(args, timeout, dry_run)
        self.run(args)

    def install_prometheus(
        self,
        release: ReleaseRef,
        service_monitor_namespaces: tuple[str, ...],
        timeout: str,
        dry_run: bool,
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
        chart = "oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack"
        chart_version = "88.5.2"
        if dry_run:
            args = [
                "template",
                release.name,
                chart,
                "--version",
                chart_version,
                "--namespace",
                release.namespace,
                "--include-crds",
            ]
        else:
            args = self._upgrade_install_args(release, chart, chart_version)
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
        if not dry_run:
            self._finish_upgrade(args, timeout, False)
        self.run(args)

    def install_dcgm_exporter(
        self,
        release: ReleaseRef,
        observability_labels: tuple[tuple[str, str], ...],
        node_selector: tuple[str, str] | None,
        reuse_values: bool,
        timeout: str,
        dry_run: bool,
    ) -> None:
        """Install or upgrade the CLI-managed NVIDIA DCGM Exporter release."""
        chart = (
            "https://nvidia.github.io/dcgm-exporter/helm-charts/"
            "dcgm-exporter-4.8.3.tgz"
        )
        metrics = """# Foretoken hardware metrics
DCGM_FI_DEV_GPU_UTIL, gauge, GPU utilization (in %).
DCGM_FI_DEV_MEM_COPY_UTIL, gauge, Memory utilization (in %).
DCGM_FI_DEV_FB_FREE, gauge, Framebuffer memory free (in MiB).
DCGM_FI_DEV_FB_USED, gauge, Framebuffer memory used (in MiB).
DCGM_FI_DEV_POWER_USAGE, gauge, Power draw (in W).
DCGM_FI_DEV_GPU_TEMP, gauge, GPU temperature (in C).
DCGM_FI_DEV_XID_ERRORS, gauge, Last XID error code.
"""
        if dry_run:
            args = [
                "template",
                release.name,
                chart,
                "--namespace",
                release.namespace,
            ]
        else:
            args = self._upgrade_install_args(release, chart, None)
            if reuse_values:
                args.append("--reuse-values")
        args.extend(
            [
                "--set",
                "serviceMonitor.enabled=true",
                "--set-string",
                "customMetrics=" + metrics.replace(",", "\\,"),
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
        if not dry_run:
            self._finish_upgrade(args, timeout, False)
        self.run(args)

    def uninstall(self, release: ReleaseRef, timeout: str) -> None:
        """Remove one release whose ownership was verified by the caller."""
        self.run(
            [
                "uninstall",
                release.name,
                "--namespace",
                release.namespace,
                "--wait",
                f"--timeout={timeout}",
            ]
        )


def _image_repository_tag(reference: str) -> tuple[str, str]:
    """Split one tagged image reference produced by the source workflow."""
    repository, separator, tag = reference.rpartition(":")
    if not separator or not repository or not tag:
        raise DeploymentError(f"source image must include a tag: {reference}")
    return repository, tag


def _decode_json(output: str) -> Any:
    """Decode one Helm JSON response without hiding invalid output."""
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("helm returned invalid JSON") from exc
