# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execute Helm and own private chart preparation, rendering, and release metadata."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:
    from yaml import SafeLoader as _SafeLoader

from foretoken.command_execution import CommandExecution, run_command
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import ResourceRef
from foretoken.network_sources import (
    select_github_download,
    select_platform_image_sources,
    select_platform_oci_reference,
)
from foretoken.platform.config import PlatformConfig
from foretoken.platform.types import ReleaseRef
from foretoken.tools import resolve_tool


class HelmCommandError(DeploymentError):
    """A completed Helm command failure, distinct from process startup or timeout errors."""

    def __init__(self, command: list[str], completed: subprocess.CompletedProcess[str]) -> None:
        self.operation = command[1]
        detail = completed.stderr.strip() or completed.stdout.strip()
        super().__init__(f"{shlex.join(command)} failed: {detail}")


class HelmClient:
    """Share command execution and chart lifetimes across platform component owners."""

    def __init__(self, config: PlatformConfig) -> None:
        self._helm = resolve_tool("helm", "FORETOKEN_HELM")
        self.config = config

    def run(
        self, args: Iterable[str], *, input_text: str | None = None, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute Helm; optional-component callers may handle a completed failure themselves."""
        command = [self._helm, *args]
        completed = run_command(command, input=input_text, text=True, capture_output=True)
        if completed.returncode and check:
            raise HelmCommandError(command, completed)
        return completed

    def upgrade_args(
        self, release: ReleaseRef, chart: str, timeout: str, *,
        labels: tuple[tuple[str, str], ...] = (), wait: bool = True,
    ) -> list[str]:
        """Build one owned chart upgrade with the caller's readiness and timeout choice."""
        args = [
            "upgrade", "--install", release.name, chart,
            "--namespace", release.namespace, "--create-namespace", "--labels",
            ",".join(f"{key}={value}" for key, value in (self.config.management_label, *labels)),
        ]
        if wait:
            args.append("--wait")
        args.append(f"--timeout={timeout}")
        return args

    @contextmanager
    def prepared_chart(self, source: str, version: str | None) -> Iterator[Path]:
        """Keep a private chart copy alive through image selection, rendering, and installation."""
        with tempfile.TemporaryDirectory(prefix="foretoken-chart-") as directory:
            if Path(source).is_dir():
                chart = Path(directory) / "chart"
                shutil.copytree(source, chart)
            else:
                preferred = self._chart_source(source, version)
                explicit_github = not source.startswith("oci://") and os.environ.get(
                    "FORETOKEN_GITHUB_MIRROR"
                )
                candidates = (preferred,) if explicit_github else tuple(
                    dict.fromkeys((preferred, source))
                )
                for index, candidate in enumerate(candidates):
                    destination = Path(directory) / str(index)
                    destination.mkdir()
                    args = ["pull", candidate, "--untar", "--untardir", str(destination)]
                    if version is not None:
                        args.extend(["--version", version])
                    try:
                        self.run(args)
                    except DeploymentError:
                        if execution := CommandExecution.current():
                            execution.check()
                        if index == len(candidates) - 1:
                            raise
                        print("Chart mirror unavailable; using the original source", flush=True)
                        continue
                    (metadata,) = destination.glob("*/Chart.yaml")
                    chart = metadata.parent
                    break
            yield chart

    def render_chart(
        self, args: list[str], *, input_text: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Render native templates with the same namespace and values as the eventual upgrade."""
        command = ["template", args[2], args[3], "--dry-run=server"]
        for index, value in enumerate(args):
            if value in {"--namespace", "--values", "--set", "--set-string", "--set-json"}:
                command.extend([value, args[index + 1]])
        rendered = self.run(command, input_text=input_text).stdout
        return tuple(
            document for document in yaml.load_all(rendered, Loader=_SafeLoader)
            if document is not None
        )

    def chart_resource(
        self, release: ReleaseRef, *, api_version: str, kind: str,
        chart_description: str,
    ) -> ResourceRef:
        """Resolve one resource from an installed component's native Helm manifest."""
        rendered = self.run(
            ["get", "manifest", release.name, "--namespace", release.namespace]
        ).stdout
        try:
            resources = [
                item for item in yaml.load_all(rendered, Loader=_SafeLoader)
                if isinstance(item, dict)
                and item.get("apiVersion") == api_version and item.get("kind") == kind
            ]
        except yaml.YAMLError as exc:
            raise DeploymentError(f"{chart_description} returned invalid YAML") from exc
        if len(resources) != 1:
            raise DeploymentError(f"{chart_description} must contain one {kind}")
        metadata = resources[0]["metadata"]
        return ResourceRef(kind, metadata["name"], metadata.get("namespace") or release.namespace)

    def set_chart_image_sources(
        self, chart: Path, paths: tuple[str, ...], *, release: ReleaseRef,
        overrides: dict[str, Any] | None = None, subcharts: tuple[str, ...] = (),
        version_prefixes: tuple[str, ...] = (), distroless_paths: tuple[str, ...] = (),
    ) -> None:
        """Resolve native image defaults, retaining installed sources and explicit overrides."""
        values, versions = self._chart_image_defaults(chart, subcharts)
        overrides = overrides or {}
        images = {}
        for path in paths:
            owner = path.split(".", 1)[0]
            if overrides.get("global", {}).get("imageRegistry"):
                continue
            try:
                explicit = value_at(overrides, path)
            except KeyError:
                explicit = {}
            if explicit is None or any(key in explicit for key in ("registry", "repository")):
                continue
            image = merge_values(value_at(values, path), explicit)
            repository = image["repository"]
            version = versions.get(owner, versions[""])
            tag = image.get("tag") or (f"v{version}" if path in version_prefixes else version)
            if path in distroless_paths and image.get("distroless"):
                tag += "-distroless"
            digest = image.get("digest") or image.get("sha")
            if digest and ":" not in digest:
                digest = f"sha256:{digest}"
            reference = f"{repository}@{digest}" if digest else f"{repository}:{tag}"
            registry = image.get("registry")
            if registry:
                reference = f"{registry}/{reference}"
            images[path] = (reference, image)
        sources = select_platform_image_sources(
            {path: reference for path, (reference, _) in images.items()},
            self.config.image_registry,
            installed_images=self.installed_chart_images(release) if images else (),
        )
        updates = {}
        for path, (reference, image) in images.items():
            selected = sources[path]
            if selected != reference:
                repository = (
                    selected.rsplit("@", 1)[0] if image.get("digest") or image.get("sha")
                    else image_repository_tag(selected)[0]
                )
                if image.get("registry"):
                    registry, repository = repository.split("/", 1)
                    updates[f"{path}.registry"] = registry
                updates[f"{path}.repository"] = repository
        self.set_chart_defaults(chart, updates)

    @staticmethod
    def set_chart_defaults(chart: Path, updates: dict[str, Any]) -> None:
        """Keep automatic choices in the private chart rather than stored user values."""
        if not updates:
            return
        path = chart / "values.yaml"
        values = yaml.safe_load(path.read_text())
        for name, value in updates.items():
            target = values
            *parents, key = name.split(".")
            for parent in parents:
                target = target.setdefault(parent, {})
            target[key] = value
        path.write_text(yaml.safe_dump(values))

    def installed_chart_manifest(self, release: ReleaseRef) -> tuple[dict[str, Any], ...]:
        """Read an installed release's choices without freezing its chart defaults."""
        if not self.release_exists(release):
            return ()
        manifest = self.run([
            "get", "manifest", release.name, "--namespace", release.namespace,
        ]).stdout
        return tuple(
            document for document in yaml.load_all(manifest, Loader=_SafeLoader)
            if document is not None
        )

    def installed_chart_images(self, release: ReleaseRef) -> set[str]:
        """Read prior workload images and images owned by Prometheus Operator resources."""
        from foretoken.build.state import application_images

        documents = self.installed_chart_manifest(release)
        images = application_images(documents)
        images.update(
            document["spec"]["image"] for document in documents
            if document["kind"] in {"Prometheus", "Alertmanager", "ThanosRuler"}
            and document["spec"].get("image")
        )
        return images

    def stored_chart_values(self, release: ReleaseRef) -> dict[str, Any]:
        """Restore optional component customizations without freezing its chart defaults."""
        return self.release_user_values(release) if self.release_exists(release) else {}

    def release_exists(self, release: ReleaseRef) -> bool:
        """Return whether a release with the fixed identity exists."""
        completed = self.run([
            "get", "metadata", release.name,
            "--namespace", release.namespace, "--output", "json",
        ], check=False)
        if completed.returncode == 0:
            return True
        detail = completed.stderr.strip() or completed.stdout.strip()
        if "release: not found" in detail:
            return False
        raise HelmCommandError(completed.args, completed)

    def is_cli_managed(self, release: ReleaseRef) -> bool:
        """Return whether Helm storage assigns the release to the current tool."""
        return self.has_release_label(release, *self.config.management_label)

    def is_cleanup_managed(self, release: ReleaseRef) -> bool:
        """Return whether the current tool may retire the release."""
        return self.is_cli_managed(release) or self.has_release_label(
            release, *self.config.legacy_management_label
        )

    def has_release_label(self, release: ReleaseRef, key: str, value: str) -> bool:
        """Return whether Helm storage carries one exact release label."""
        return bool(self.list_releases(
            release.name, namespace=release.namespace, selector=f"{key}={value}",
        ))

    def release_values(self, release: ReleaseRef) -> dict[str, Any]:
        """Return the effective values stored for one Helm release."""
        return self._get_release_values(release, include_defaults=True)

    def release_user_values(self, release: ReleaseRef) -> dict[str, Any]:
        """Return supplied values for the installed release."""
        return self._get_release_values(release, include_defaults=False)

    def release_install_source(self, release: ReleaseRef) -> str:
        """Read the required installation source recorded on a platform release."""
        sources = tuple(
            source for source in ("release", "source")
            if self.has_release_label(release, self.config.install_source_label, source)
        )
        if len(sources) != 1:
            raise DeploymentError(
                f"Helm release {release.display_name} has no valid {self.config.install_source_label} label"
            )
        return sources[0]

    def list_releases(
        self, name: str, *, namespace: str | None, selector: str = "",
    ) -> tuple[dict[str, Any], ...]:
        """List an exact release name in one namespace or across the cluster."""
        args = [
            "list", "--filter", f"^{re.escape(name)}$", "--output", "json", "--max", "0",
            *(["--all-namespaces"] if namespace is None else ["--namespace", namespace]),
        ]
        if selector:
            args.extend(["--selector", selector])
        listed = _decode_json(self.run(args).stdout)
        if not isinstance(listed, list) or not all(isinstance(item, dict) for item in listed):
            raise DeploymentError("helm list returned an unexpected JSON value")
        return tuple(listed)

    def uninstall(self, release: ReleaseRef, timeout: str) -> None:
        """Remove a release whose ownership was verified by its lifecycle caller."""
        self.run([
            "uninstall", release.name, "--namespace", release.namespace,
            "--wait", f"--timeout={timeout}",
        ])

    def _get_release_values(self, release: ReleaseRef, *, include_defaults: bool) -> dict[str, Any]:
        """Read stored values with the caller's requested default scope."""
        args = ["get", "values", release.name, "--namespace", release.namespace]
        if include_defaults:
            args.append("--all")
        values = _decode_json(self.run([*args, "--output", "json"]).stdout)
        if values is None and not include_defaults:
            return {}
        if not isinstance(values, dict):
            raise DeploymentError("helm get values returned an unexpected JSON value")
        return values

    def _chart_source(self, source: str, version: str | None) -> str:
        """Resolve a managed chart source without overriding explicit registry choices."""
        if self.config.image_registry is not None:
            return source
        if not source.startswith("oci://"):
            return select_github_download(source, os.environ.get("FORETOKEN_GITHUB_MIRROR"))
        reference = f"{source}:{version}" if version is not None else source
        selected = select_platform_oci_reference(reference)
        return selected.removesuffix(f":{version}") if version is not None else selected

    def _chart_image_defaults(
        self, chart: Path, subcharts: tuple[str, ...],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Read native image defaults and app versions without interpreting templates."""
        values = yaml.safe_load((chart / "values.yaml").read_text())
        metadata = yaml.safe_load((chart / "Chart.yaml").read_text())
        versions = {"": str(metadata["appVersion"])}
        for name in subcharts:
            dependency = chart / "charts" / name
            if not dependency.is_dir():
                (dependency,) = (chart / "charts").glob(f"{name}-*.tgz")
            child = yaml.safe_load(self.run(["show", "values", str(dependency)]).stdout)
            child_metadata = yaml.safe_load(self.run(["show", "chart", str(dependency)]).stdout)
            values[name] = merge_values(child, values.get(name, {}))
            versions[name] = str(child_metadata["appVersion"])
        return values, versions


def owned_by_release(document: dict[str, Any], release: ReleaseRef) -> bool:
    """Match Helm ownership before platform lifecycles prepare or retire native resources."""
    metadata = document["metadata"]
    annotations = metadata.get("annotations", {})
    return (
        metadata.get("labels", {}).get("app.kubernetes.io/managed-by") == "Helm"
        and annotations.get("meta.helm.sh/release-name") == release.name
        and annotations.get("meta.helm.sh/release-namespace") == release.namespace
    )


def value_at(values: dict[str, Any], path: str) -> Any:
    """Read a field whose shape is owned by the selected native chart."""
    value: Any = values
    for key in path.split("."):
        value = value[key]
    return value


def merge_values(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Overlay mappings for native image selection; Helm owns rendering and validation."""
    merged = defaults.copy()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def image_repository_tag(reference: str) -> tuple[str, str]:
    """Split an image reference, retaining Docker's implicit latest tag when omitted."""
    if ":" not in reference.rsplit("/", 1)[-1]:
        return reference, "latest"
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
