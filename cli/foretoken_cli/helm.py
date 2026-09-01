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
        release: ReleaseRef, chart: str, chart_version: str
    ) -> list[str]:
        """Build the shared CLI-owned Helm release identity and chart selection."""
        management_key, management_value = release.management_label
        return [
            "upgrade",
            "--install",
            release.name,
            chart,
            "--version",
            chart_version,
            "--namespace",
            release.namespace,
            "--create-namespace",
            "--labels",
            f"{management_key}={management_value}",
        ]

    @staticmethod
    def _finish_upgrade(args: list[str], timeout: str, dry_run: bool) -> None:
        """Add the execution mode shared by managed Helm upgrades."""
        if dry_run:
            args.extend(["--dry-run=server", "--hide-secret"])
        else:
            args.extend(["--wait", f"--timeout={timeout}"])

    def install_platform(
        self,
        *,
        release: ReleaseRef,
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
        chart = "oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken"
        chart_version = version("foretoken-cli")
        render_template = dry_run and bool(dry_run_api_versions)
        if render_template:
            args = [
                "template",
                release.name,
                chart,
                "--version",
                chart_version,
                "--namespace",
                release.namespace,
            ]
        else:
            args = self._upgrade_install_args(release, chart, chart_version)
        if reuse_values and not render_template:
            args.append("--reuse-values")
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
        if render_template:
            for api_version in dry_run_api_versions:
                args.extend(["--api-versions", api_version])
        else:
            self._finish_upgrade(args, timeout, dry_run)
        self.run(args)

    def install_prometheus(
        self, release: ReleaseRef, timeout: str, dry_run: bool
    ) -> None:
        """Install or upgrade the CLI-managed kube-prometheus-stack release."""
        namespace_selector = {
            "matchLabels": {
                "kubernetes.io/metadata.name": release.namespace,
            }
        }
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


def _decode_json(output: str) -> Any:
    """Decode one Helm JSON response without hiding invalid output."""
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("helm returned invalid JSON") from exc
