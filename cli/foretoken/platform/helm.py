# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Render, install, and inspect the Foretoken platform release."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foretoken.accelerators.config import GPU_RESOURCE_BACKENDS
from foretoken.applications.files import application_references
from foretoken.downloads.sources import select_platform_image_sources
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.kubernetes.documents import decode_resources
from foretoken.platform.config import load_platform_values
from foretoken.platform.helm_client import (
    HelmClient,
    image_repository_tag,
    merge_values,
    owned_by_release,
    value_at,
)
from foretoken.platform.storage.directories import prepare_directory_bindings
from foretoken.platform.types import ReleaseRef

if TYPE_CHECKING:
    from foretoken.build.source import SourceImages
    from foretoken.platform.storage.application_files import PlatformApplicationFiles


class Helm(HelmClient):
    """Own the platform chart contract while component lifecycles own their dependencies."""

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
        observability_prometheus: str,
        grafana_anonymous_access: bool | None,
        log_endpoint: str,
        log_storage_statefulset: str,
        gpu_resource_name: str | None,
        rdma_resource_name: str | None,
        rdma_managed: bool,
        rdma_node_names: tuple[str, ...],
        download_proxy_url: str,
        stored_values: dict[str, Any] | None,
        kubectl: Kubectl,
        application_files: PlatformApplicationFiles,
        timeout: str,
    ) -> None:
        """Install resolved platform intent while storage owners retain bootstrap and publication lifetimes."""
        source_mode = source_images is not None
        chart = (
            str(source_images.inputs / "deploy" / "charts" / "foretoken")
            if source_images is not None else self.config.platform.source
        )
        chart_version = None if source_mode else self.config.platform.version
        args = self.upgrade_args(
            release, chart, timeout,
            labels=((self.config.install_source_label, "source" if source_mode else "release"),),
        )
        if stored_values is not None:
            # Reusing Helm's original values would restore keys removed by migration.
            args.extend(["--reset-values", "--values", "-"])
        self._add_platform_values(
            args, values, frontend_mode, gateway_name, gateway_namespace,
            gateway_section_name, gateway_controller_name, observability_labels,
        )
        args.extend(["--set", f"development.enabled={str(source_mode).lower()}"])
        args.extend(["--set-string", f"observability.prometheus={observability_prometheus}"])
        args.extend(["--set-string", f"observability.logs.datasourceURL={log_endpoint}"])
        args.extend(["--set-string", f"observability.logs.managedStatefulSet={log_storage_statefulset}"])
        if grafana_anonymous_access is not None:
            args.extend([
                "--set-json", "observability.grafana.anonymousAccess=" + json.dumps(grafana_anonymous_access),
            ])
        if gpu_resource_name is not None:
            args.extend(["--set-string", f"runtime.vllm.gpu.resourceName={gpu_resource_name}"])
        self._add_download_proxy(args, download_proxy_url)
        if rdma_managed:
            args.extend([
                "--set", "rdma.managed=true", "--set-json", "rdma.nodeNames=" + json.dumps(rdma_node_names),
            ])
        if rdma_resource_name is not None:
            args.extend(["--set-string", f"rdma.resourceName={rdma_resource_name}"])
        # Blanket registry overrides would also rewrite user and source images.
        args.extend(["--set-string", "global.imageRegistry="])
        if source_images is not None:
            repository, tag = image_repository_tag(source_images.control_plane)
            args.extend([
                "--set-string", "controller.applicationURL=" + source_images.applications["control-plane"],
                "--set-string", "frontend.applicationURL=" + source_images.applications["frontend"],
                "--set-string", "runtime.vllm.applicationURL=" + source_images.applications["model-server"],
                "--set-string", "applicationFiles.releaseURL=",
                "--set-string", f"image.repository={repository}",
                "--set-string", f"image.tag={tag}", "--set-string", "image.digest=",
                "--set", "image.pullPolicy=" + ("Never" if source_images.image_mode == "import" else "IfNotPresent"),
                "--set-string", f"frontend.image={source_images.frontend}",
                "--set-string", f"runtime.vllm.image={source_images.model_server}",
            ])
        input_text = json.dumps(stored_values) if stored_values is not None else None
        overrides = stored_values or {}
        for value in load_platform_values(values):
            overrides = merge_values(overrides, value)
        with self.prepared_chart(chart, chart_version) as prepared:
            args[3] = str(prepared)
            self._set_platform_image_sources(args, overrides, source_mode, input_text)
            configuration = next(
                document for document in self.render_chart(args, input_text=input_text)
                if document["kind"] == "ConfigMap"
                and document["metadata"].get("labels", {}).get("foretoken.io/platform") == "configuration"
            )
            if "deployment-template.json" not in configuration["data"]:
                raise DeploymentError("The selected platform chart does not support deployment instances")
            template = json.loads(configuration["data"]["deployment-template.json"])
            directories = prepare_directory_bindings(
                kubectl, template["directories"],
                source_images.source_root if source_images is not None else Path.cwd(),
                release.name + "-data", timeout,
            )
            args.extend(["--set-json", "workload.cache.directories=" + json.dumps(directories)])
            # Adoption keeps the origin publisher alive until Helm selects the complete release.
            with application_files.adopt(self.render_chart(args, input_text=input_text), timeout):
                self.run(args, input_text=input_text)
                self.set_platform_retiring(
                    kubectl, self.platform_configuration(), retiring=False,
                    clear_source_build=not source_mode,
                )

    def platform_release(self) -> ReleaseRef:
        """Return the Foretoken release identity shared by installation and source consumers."""
        return ReleaseRef(self.config.platform.release_name, self.config.namespace)

    def platform_configuration(self) -> dict[str, Any]:
        """Read the installed platform identity and resolved defaults for deployment and source owners."""
        release = self.platform_release()
        configuration = self._kubectl.get_if_exists(
            "configmap", f"{release.name}-platform", release.namespace,
        )
        if configuration is None:
            raise DeploymentError("Foretoken platform is not installed; run foretoken install first")
        if not owned_by_release(configuration, release):
            raise DeploymentError(f"Platform configuration is not owned by {release.display_name}")
        return configuration

    def set_platform_retiring(
        self, kubectl: Kubectl, configuration: dict[str, Any], *, retiring: bool,
        clear_source_build: bool = False,
    ) -> dict[str, Any]:
        """Change instance acceptance with native version preconditions and retire obsolete source recipes."""
        from foretoken.build.records import SOURCE_BUILD_KEY

        metadata = configuration["metadata"]
        annotation = "inference.foretoken.io/retiring"
        is_retiring = metadata.get("annotations", {}).get(annotation) == "true"
        remove_recipe = clear_source_build and SOURCE_BUILD_KEY in configuration.get("data", {})
        if is_retiring == retiring and not remove_recipe:
            return configuration
        patch: dict[str, Any] = {
            "metadata": {
                "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
                "annotations": {annotation: "true" if retiring else None},
            },
        }
        if remove_recipe:
            patch["data"] = {SOURCE_BUILD_KEY: None}
        return json.loads(kubectl.run([
            "patch", "configmap", metadata["name"], "--namespace", metadata["namespace"],
            "--type=merge", "--output=json", "--patch", json.dumps(patch),
        ]).stdout)

    def render_source_origin(
        self, root: Path, values: tuple[str, ...], timeout: str,
        *, stored_values: dict[str, Any] | None, download_proxy_url: str,
    ) -> tuple[dict[str, Any], ...]:
        """Render bootstrap storage, leaving user values interpretation to the selected Helm chart."""
        args = self.upgrade_args(self.platform_release(), str(root / "deploy/charts/foretoken"), timeout)
        merged = stored_values or {}
        for value in load_platform_values(values):
            merged = merge_values(merged, value)
        input_text = json.dumps(stored_values) if stored_values is not None else None
        if stored_values is not None:
            args.extend(["--values", "-"])
        for path in values:
            args.extend(["--values", path])
        args.extend([
            "--set", "development.enabled=true",
            "--set-string", "observability.mode=disabled", "--set-string", "applicationFiles.releaseURL=",
        ])
        self._add_download_proxy(args, download_proxy_url)
        with self.prepared_chart(args[3], None) as chart:
            args[3] = str(chart)
            self._set_platform_image_sources(args, merged, True, input_text)
            return self.render_chart(args, input_text=input_text)

    def stop_platform_tasks(self, kubectl: Kubectl, timeout: str) -> None:
        """Stop native maintenance and failed bootstrap jobs before retiring their files and storage."""
        release = self.platform_release()
        if not kubectl.exists("namespace", release.namespace):
            return
        for resource in kubectl.list_resources(
            ("cronjobs", "jobs"), release.namespace, label_selector="foretoken.io/platform-task=true",
        ):
            if owned_by_release(resource, release):
                kubectl.run([
                    "delete", resource["kind"], resource["metadata"]["name"],
                    "--namespace", release.namespace, "--cascade=foreground", "--wait=true", f"--timeout={timeout}",
                ])

    def application_history(self) -> set[str] | None:
        """Return application references retained by the platform's Helm history."""
        return self._history_references(application_references)

    def image_history(self) -> set[str] | None:
        """Return images retained by platform defaults and Helm rollback history."""
        from foretoken.build.state import application_images

        return self._history_references(application_images)

    @staticmethod
    def _add_download_proxy(args: list[str], proxy_url: str) -> None:
        """Project the download endpoint into bootstrap and final platform renders."""
        args.extend(["--set-string", f"modelDistribution.proxyURL={proxy_url}"])

    @staticmethod
    def _add_platform_values(
        args: list[str], values: tuple[str, ...], frontend_mode: str | None,
        gateway_name: str, gateway_namespace: str, gateway_section_name: str,
        gateway_controller_name: str, observability_labels: tuple[tuple[str, str], ...],
    ) -> None:
        """Render the platform choices shared by release and source installations."""
        for values_file in values:
            args.extend(["--values", values_file])
        args.extend(["--set", "frontend.enabled=true", "--set", "observability.mode=enabled"])
        if observability_labels:
            args.extend([
                "--set-json", "observability.additionalLabels=" + json.dumps(dict(observability_labels), separators=(",", ":")),
            ])
        if frontend_mode is not None:
            gateway_create = frontend_mode == "gateway" and not gateway_name
            args.extend(["--set", f"frontend.mode={frontend_mode}"])
            args.extend(["--set", f"frontend.gateway.create={str(gateway_create).lower()}"])
        if gateway_controller_name:
            args.extend(["--set-string", f"frontend.gateway.controllerName={gateway_controller_name}"])
        if gateway_name:
            args.extend(["--set-string", f"frontend.gateway.name={gateway_name}"])
        if gateway_namespace:
            args.extend(["--set-string", f"frontend.gateway.namespace={gateway_namespace}"])
        if gateway_section_name or (frontend_mode == "gateway" and gateway_name):
            args.extend(["--set-string", f"frontend.gateway.sectionName={gateway_section_name}"])

    @staticmethod
    def _platform_images(documents: tuple[dict[str, Any], ...]) -> tuple[dict[str, str], str | None]:
        """Read native platform images and backend identity, including retained release layouts."""
        images: dict[str, str] = {}
        runtime_backend = None
        for document in documents:
            if document["kind"] == "ConfigMap" and document["metadata"].get("labels", {}).get("foretoken.io/platform") == "configuration":
                values = json.loads(document["data"]["deploymentValues"])
                image = values["image"]
                images["image.repository"] = (
                    f"{image['repository']}@{image['digest']}" if image["digest"]
                    else f"{image['repository']}:{image['tag']}"
                )
                images["frontend.image"] = values["frontend"]["image"]
                images["runtime.vllm.image"] = values["runtime"]["vllm"]["image"]
                images["applicationFiles.clientImage"] = values["applicationFiles"]["clientImage"]
                runtime_backend = GPU_RESOURCE_BACKENDS.get(values["runtime"]["vllm"]["gpu"]["resourceName"])
            if (
                document["kind"] == "ConfigMap"
                and document["metadata"].get("labels", {}).get("foretoken.io/profile-viewer") == "configuration"
                and document["data"]["nsightImage"]
            ):
                for key, path in (("nsightImage", "profiling.nsightViewerImage"), ("proxyImage", "profiling.viewerProxyImage")):
                    if reference := document["data"][key]:
                        images[path] = reference
            if document["kind"] not in {"Deployment", "DaemonSet", "StatefulSet"}:
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                labels = document["metadata"].get("labels", {})
                if container["name"] == "files" and labels.get("foretoken.io/application-files") == "server":
                    images["applicationFiles.serverImage"] = container["image"]
                if container["name"] == "valkey" and labels.get("app.kubernetes.io/name") in {"foretoken-admission-store", "foretoken-response-store"}:
                    images["admissionStore.image"] = container["image"]
                if container["name"] == "rdma-device-plugin":
                    images["rdma.image"] = container["image"]
                if container["name"] == "manager":
                    images["image.repository"] = container["image"]
                    for argument in container.get("args", ()):
                        if argument.startswith("--application-files="):
                            application = json.loads(argument.removeprefix("--application-files="))
                            images["applicationFiles.clientImage"] = application["image"]
                        if argument.startswith("--gpu-resource-name="):
                            runtime_backend = GPU_RESOURCE_BACKENDS.get(argument.removeprefix("--gpu-resource-name="))
                        for prefix, path in (("--frontend-image=", "frontend.image"), ("--inference-engine-image=", "runtime.vllm.image")):
                            if argument.startswith(prefix):
                                images[path] = argument.removeprefix(prefix)
        return images, runtime_backend

    def _set_platform_image_sources(
        self, args: list[str], overrides: dict[str, Any], source_mode: bool,
        input_text: str | None,
    ) -> None:
        """Resolve platform defaults while preserving explicit and installed image sources."""
        images, runtime_backend = self._platform_images(self.render_chart(args, input_text=input_text))
        previous, _ = self._platform_images(self.installed_chart_manifest(self.platform_release()))
        references = {}
        prior_sources = {}
        for path, reference in images.items():
            if source_mode and path in {"image.repository", "frontend.image", "runtime.vllm.image"}:
                continue
            try:
                explicit = value_at(overrides, path)
            except KeyError:
                explicit = None
            if explicit is not None and explicit != "auto":
                continue
            prior = previous.get(path)
            if path == "runtime.vllm.image":
                if runtime_backend is None:
                    continue
                path = f"runtime.vllm.environmentImages.{runtime_backend}"
                try:
                    if value_at(overrides, path):
                        continue
                except KeyError:
                    pass
            references[path] = reference
            if prior is not None:
                prior_sources[path] = prior
        sources = select_platform_image_sources(references, self.config.image_registry, previous=prior_sources)
        updates = {}
        for path, selected in sources.items():
            if selected == references[path]:
                continue
            if path == "image.repository":
                selected = selected.split("@", 1)[0] if "@" in selected else image_repository_tag(selected)[0]
            updates[path] = selected
        self.set_chart_defaults(Path(args[3]), updates)

    def _history_references(self, extract: Callable[[Iterable[dict[str, Any]]], set[str]]) -> set[str] | None:
        """Read rollback references, deferring collection when the platform revision advances."""
        release = self.platform_release()
        if not self.release_exists(release):
            return set()
        status = json.loads(self.run([
            "status", release.name, "--namespace", release.namespace, "--output", "json",
        ]).stdout)
        revision = int(status["version"])
        history = json.loads(self.run([
            "history", release.name, "--namespace", release.namespace, "--max", str(revision), "--output", "json",
        ]).stdout)
        if any(int(item["revision"]) > revision for item in history):
            return None
        references = set()
        for item in history:
            manifest = self.run([
                "get", "manifest", release.name, "--namespace", release.namespace,
                "--revision", str(item["revision"]),
            ]).stdout
            references.update(extract(decode_resources(manifest)))
        return references
