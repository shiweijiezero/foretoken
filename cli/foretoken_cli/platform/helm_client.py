# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Low-level Helm command execution and release metadata queries."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from typing import Any, NoReturn

from foretoken_cli.manifest import DeploymentError
from foretoken_cli.platform.config import PlatformConfig
from foretoken_cli.platform.types import ReleaseRef


class HelmClient:
    """Execute Helm commands and read release-owned metadata."""

    def __init__(self, config: PlatformConfig) -> None:
        if shutil.which("helm") is None:
            raise DeploymentError("helm is required to install the Foretoken platform")
        self._config = config

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
        return bool(
            self._list_releases(
                release,
                selector="=".join(self._config.management_label),
            )
        )

    def managed_envoy_gateway_releases(self) -> tuple[ReleaseRef, ...]:
        """Return CLI-managed Envoy Gateway releases across the cluster."""
        listed = _decode_json(
            self.run(
                [
                    "list",
                    "--all",
                    "--all-namespaces",
                    "--filter",
                    f"^{re.escape(self._config.envoy_gateway.release_name)}$",
                    "--selector",
                    "=".join(self._config.management_label),
                    "--output",
                    "json",
                ]
            ).stdout
        )
        if not isinstance(listed, list) or not all(
            isinstance(item, dict) for item in listed
        ):
            raise DeploymentError("helm list returned an unexpected JSON value")
        return tuple(
            ReleaseRef(str(item.get("name") or ""), str(item.get("namespace") or ""))
            for item in listed
            if item.get("name") and item.get("namespace")
        )

    def has_release_label(
        self, release: ReleaseRef, key: str, value: str
    ) -> bool:
        """Return whether Helm storage carries one exact release label."""
        return bool(
            self._list_releases(release, selector=f"{key}={value}")
        )

    def _release_values(self, release: ReleaseRef) -> dict[str, Any]:
        """Return the effective values stored for one Helm release."""
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
        return values

    def release_install_source(self, release: ReleaseRef) -> str:
        """Return the required installation source recorded on a platform release."""
        sources = tuple(
            source
            for source in ("release", "source")
            if self.has_release_label(
                release, self._config.install_source_label, source
            )
        )
        if len(sources) != 1:
            raise DeploymentError(
                f"Helm release {release.display_name} has no valid "
                f"{self._config.install_source_label} label"
            )
        return sources[0]

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
