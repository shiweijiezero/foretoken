# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Helm chart adapters for CLI-managed Foretoken platform releases."""

from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml

from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.network_sources import (
    platform_image_reference,
    select_platform_oci_reference,
)
from foretoken.platform.config import (
    load_balancer_config_from_values,
    load_platform_values,
)
from foretoken.platform.helm_client import HelmClient
from foretoken.platform.types import (
    LoadBalancerConfig,
    PlatformGatewayConfig,
    ReleaseRef,
)
from foretoken.source import SourceImages


class Helm(HelmClient):
    """Build and execute Helm operations for platform-owned charts."""

    def _chart_source(self, source: str, version: str | None) -> str:
        """Resolve a managed OCI chart without overriding an explicit mirror."""
        if self._config.image_registry is not None or not source.startswith("oci://"):
            return source
        reference = f"{source}:{version}" if version is not None else source
        selected = select_platform_oci_reference(reference)
        return selected.removesuffix(f":{version}") if version is not None else selected

    def _chart_image_defaults(
        self, args: list[str], subcharts: tuple[str, ...]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Read native image defaults and app versions, including packaged subcharts."""
        chart_args = [args[3]]
        if "--version" in args:
            chart_args.extend(["--version", args[args.index("--version") + 1]])
        if not subcharts:
            values = yaml.safe_load(self.run(["show", "values", *chart_args]).stdout)
            metadata = yaml.safe_load(self.run(["show", "chart", *chart_args]).stdout)
            return values, {"": str(metadata["appVersion"])}
        # Helm show values omits dependency defaults. Read the named packaged
        # dependencies without extracting files; Helm still owns actual rendering.
        with tempfile.TemporaryDirectory(prefix="foretoken-chart-images-") as directory:
            self.run(["pull", *chart_args, "--destination", directory])
            archive_path, = Path(directory).glob("*.tgz")
            with tarfile.open(archive_path) as archive:
                metadata_path = next(
                    name for name in archive.getnames()
                    if name.count("/") == 1 and name.endswith("/Chart.yaml")
                )
                root = metadata_path.removesuffix("Chart.yaml")
                metadata = yaml.safe_load(archive.extractfile(metadata_path))
                values = yaml.safe_load(archive.extractfile(root + "values.yaml"))
                versions = {"": str(metadata["appVersion"])}
                for name in subcharts:
                    prefix = f"{root}charts/{name}/"
                    child = yaml.safe_load(archive.extractfile(prefix + "values.yaml"))
                    child_metadata = yaml.safe_load(archive.extractfile(prefix + "Chart.yaml"))
                    values[name] = _merge_values(child, values.get(name, {}))
                    versions[name] = str(child_metadata["appVersion"])
                return values, versions

    def _add_chart_image_sources(
        self,
        args: list[str],
        paths: tuple[str, ...],
        *,
        subcharts: tuple[str, ...] = (),
        version_prefixes: tuple[str, ...] = (),
    ) -> None:
        """Override named native image fields without changing tags or digest fields.

        Values are applied before Helm rendering so admission hooks, sidecars and
        Operator-created workloads receive the same selection as controllers.
        """
        values, versions = self._chart_image_defaults(args, subcharts)
        for path in paths:
            image = _value_at(values, path)
            repository = image["repository"]
            owner = path.split(".", 1)[0]
            version = versions.get(owner, versions[""])
            tag = image.get("tag") or (
                f"v{version}" if path in version_prefixes else version
            )
            if path == "prometheus-node-exporter.image" and image.get("distroless"):
                tag += "-distroless"
            digest = image.get("digest") or image.get("sha")
            if digest and ":" not in digest:
                digest = f"sha256:{digest}"
            reference = f"{repository}@{digest}" if digest else f"{repository}:{tag}"
            registry = image.get("registry")
            if registry:
                reference = f"{registry}/{reference}"
            selected = platform_image_reference(reference, self._config.image_registry)
            if selected != reference:
                repository = (
                    selected.rsplit("@", 1)[0]
                    if digest else _image_repository_tag(selected)[0]
                )
                if registry:
                    registry, repository = repository.split("/", 1)
                    args.extend(["--set-string", f"{path}.registry={registry}"])
                args.extend(["--set-string", f"{path}.repository={repository}"])

    def _render_chart(
        self, args: list[str], *, input_text: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Resolve image defaults through native templates with the install values."""
        command = ["template", args[2], args[3]]
        for index, value in enumerate(args):
            if value in {
                "--version", "--namespace", "--values", "--set", "--set-string", "--set-json"
            }:
                command.extend([value, args[index + 1]])
        rendered = self.run(command, input_text=input_text).stdout
        return tuple(
            document for document in yaml.safe_load_all(rendered) if document is not None
        )

    def platform_release(self) -> ReleaseRef:
        """Return the single Foretoken platform release managed by the CLI."""
        return ReleaseRef(self._config.platform.release_name, self._config.namespace)

    def prometheus_release(self) -> ReleaseRef:
        """Return the Prometheus release managed with the platform."""
        return ReleaseRef(self._config.prometheus.release_name, self._config.namespace)

    def prometheus_resource(self, release: ReleaseRef) -> ResourceRef:
        """Read the Prometheus identity from the managed chart instead of reproducing its naming rules."""
        rendered = self.run(
            ["get", "manifest", release.name, "--namespace", release.namespace]
        ).stdout
        try:
            resources = [
                item for item in yaml.safe_load_all(rendered)
                if isinstance(item, dict)
                and item.get("apiVersion") == "monitoring.coreos.com/v1"
                and item.get("kind") == "Prometheus"
            ]
        except yaml.YAMLError as exc:
            raise DeploymentError("managed monitoring chart returned invalid YAML") from exc
        if len(resources) != 1:
            raise DeploymentError("managed monitoring chart must contain one Prometheus")
        metadata = resources[0]["metadata"]
        return ResourceRef("Prometheus", metadata["name"], metadata.get("namespace") or release.namespace)

    def dcgm_release(self) -> ReleaseRef:
        """Return the NVIDIA exporter release managed with the platform."""
        return ReleaseRef(
            self._config.dcgm_exporter.release_name, self._config.namespace
        )

    def dcgm_resource(self, release: ReleaseRef) -> ResourceRef:
        """Read the managed DCGM Exporter DaemonSet identity from Helm."""
        rendered = self.run(
            ["get", "manifest", release.name, "--namespace", release.namespace]
        ).stdout
        try:
            resources = [
                item
                for item in yaml.safe_load_all(rendered)
                if isinstance(item, dict)
                and item.get("apiVersion") == "apps/v1"
                and item.get("kind") == "DaemonSet"
            ]
        except yaml.YAMLError as exc:
            raise DeploymentError(
                "managed DCGM Exporter chart returned invalid YAML"
            ) from exc
        if len(resources) != 1:
            raise DeploymentError(
                "managed DCGM Exporter chart must contain one DaemonSet"
            )
        metadata = resources[0]["metadata"]
        return ResourceRef(
            "DaemonSet",
            metadata["name"],
            metadata.get("namespace") or release.namespace,
        )

    def envoy_gateway_release(self) -> ReleaseRef:
        """Return the Envoy Gateway release managed with the platform."""
        return ReleaseRef(
            self._config.envoy_gateway.release_name, self._config.namespace
        )

    def metallb_release(self) -> ReleaseRef:
        """Return the MetalLB release managed with the platform."""
        return ReleaseRef(
            self._config.metallb.release_name,
            self._config.load_balancer_namespace,
        )

    def leader_worker_release(self) -> ReleaseRef:
        """Return the LeaderWorkerSet controller release managed with the platform."""
        return ReleaseRef(self._config.leader_worker.release_name, self._config.namespace)

    def leader_worker_crds(self) -> str:
        """Read the CRDs shipped with the selected LeaderWorkerSet chart for upgrades."""
        chart = self._config.leader_worker
        args = ["show", "crds", self._chart_source(chart.source, chart.version)]
        if chart.version is not None:
            args.extend(["--version", chart.version])
        return self.run(args).stdout

    def install_leader_worker(self, release: ReleaseRef, timeout: str) -> None:
        """Install or upgrade the CLI-owned LeaderWorkerSet controller."""
        chart = self._config.leader_worker
        args = self._upgrade_install_args(release, chart.source, chart.version)
        self._add_chart_image_sources(args, ("image.manager",))
        self._finish_upgrade(args, timeout)
        self.run(args)

    @property
    def platform_selector_labels(self) -> tuple[tuple[str, str], ...]:
        """Return labels shared by resources in the platform release."""
        return self._config.platform_selector_labels

    @property
    def management_label(self) -> tuple[str, str]:
        """Return the label used on CLI-owned Kubernetes resources."""
        return self._config.management_label

    @property
    def envoy_gateway_default_controller(self) -> str:
        """Return the upstream Envoy Gateway controller identity."""
        return self._config.envoy_gateway_default_controller

    @property
    def envoy_gateway_controller(self) -> str:
        """Return the controller identity reserved for managed Envoy Gateway."""
        return self._config.envoy_gateway_controller

    def stored_load_balancer_config(self, release: ReleaseRef) -> LoadBalancerConfig:
        """Return the address pool stored with the managed MetalLB release."""
        return (
            load_balancer_config_from_values(self._release_values(release))
            or LoadBalancerConfig()
        )

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

    def platform_runtime_image(
        self, source_root: Path, gpu_resource_name: str
    ) -> str:
        """Render the source chart's official runtime image for one GPU resource."""
        chart = str(source_root / "deploy" / "charts" / "foretoken")
        rendered = self.run(
            [
                "template",
                "foretoken-runtime-image",
                chart,
                "--set",
                "observability.mode=disabled",
                "--set-string",
                f"runtime.vllm.gpu.resourceName={gpu_resource_name}",
            ]
        ).stdout
        try:
            documents: Any = yaml.safe_load_all(rendered)
            for document in documents:
                if not isinstance(document, dict) or document.get("kind") != "Deployment":
                    continue
                pod_spec = ((document.get("spec") or {}).get("template") or {}).get(
                    "spec"
                ) or {}
                for container in pod_spec.get("containers") or []:
                    if not isinstance(container, dict) or container.get("name") != "manager":
                        continue
                    for argument in container.get("args") or []:
                        if isinstance(argument, str) and argument.startswith(
                            "--inference-engine-image="
                        ):
                            return argument.removeprefix("--inference-engine-image=")
        except yaml.YAMLError as exc:
            raise DeploymentError(
                "platform chart rendered invalid runtime configuration"
            ) from exc
        raise DeploymentError("platform chart rendered no vLLM runtime image")

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
            self._chart_source(chart, chart_version),
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

    def _add_platform_image_sources(
        self,
        args: list[str],
        overrides: dict[str, Any],
        source_images: SourceImages | None,
        input_text: str | None,
    ) -> None:
        """Resolve native platform defaults while preserving explicit image choices."""
        images: dict[str, str] = {}
        for document in self._render_chart(args, input_text=input_text):
            if (
                document["kind"] == "ConfigMap"
                and document["metadata"].get("labels", {}).get("foretoken.io/profile-viewer")
                == "configuration"
                and document["data"]["nsightImage"]
            ):
                for key, path in (
                    ("nsightImage", "profiling.nsightViewerImage"),
                    ("proxyImage", "profiling.viewerProxyImage"),
                ):
                    if reference := document["data"][key]:
                        images[path] = reference
            if document["kind"] not in {"Deployment", "DaemonSet"}:
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                if container["name"] == "rdma-device-plugin":
                    images["rdma.image"] = container["image"]
                if container["name"] == "manager":
                    images["image.repository"] = container["image"]
                    for argument in container.get("args", ()):
                        for prefix, path in (
                            ("--frontend-image=", "frontend.image"),
                            ("--inference-engine-image=", "runtime.vllm.image"),
                        ):
                            if argument.startswith(prefix):
                                images[path] = argument.removeprefix(prefix)
        for path, reference in images.items():
            if source_images is not None and path in {
                "image.repository", "frontend.image", "runtime.vllm.image"
            }:
                continue
            try:
                explicit = _value_at(overrides, path)
            except KeyError:
                explicit = None
            if explicit is not None and explicit != "auto":
                continue
            selected = platform_image_reference(reference, self._config.image_registry)
            if selected == reference:
                continue
            if path == "image.repository":
                selected = (
                    selected.split("@", 1)[0]
                    if "@" in selected else _image_repository_tag(selected)[0]
                )
            args.extend(["--set-string", f"{path}={selected}"])

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
        gpu_resource_name: str | None,
        rdma_resource_name: str | None,
        rdma_managed: bool,
        rdma_node_names: tuple[str, ...],
        stored_values: dict[str, Any] | None,
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
        if stored_values is not None:
            # Restored user values have already been migrated. Reusing Helm's
            # original values would resurrect keys removed from the chart schema.
            args.extend(["--reset-values", "--values", "-"])
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
        args.extend(["--set-string", f"observability.prometheus={observability_prometheus}"])
        if gpu_resource_name is not None:
            args.extend(
                [
                    "--set-string",
                    f"runtime.vllm.gpu.resourceName={gpu_resource_name}",
                ]
            )
        if rdma_managed:
            args.extend(
                [
                    "--set", "rdma.managed=true",
                    "--set-json", "rdma.nodeNames=" + json.dumps(rdma_node_names),
                ]
            )
        if rdma_resource_name is not None:
            args.extend(
                ["--set-string", f"rdma.resourceName={rdma_resource_name}"]
            )
        # Individual values retain their own source registry and explicit overrides.
        # A blanket registry override would also rewrite user and source images.
        args.extend(["--set-string", "global.imageRegistry="])
        if source_images is not None:
            control_plane_image = source_images.control_plane
            frontend_image = source_images.frontend
            model_server_image = source_images.model_server
            if stored_values is not None and not all(
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
        input_text = yaml.safe_dump(stored_values) if stored_values is not None else None
        overrides = stored_values or {}
        for value in load_platform_values(values):
            overrides = _merge_values(overrides, value)
        self._add_platform_image_sources(args, overrides, source_images, input_text)
        self._finish_upgrade(args, timeout)
        self.run(args, input_text=input_text)

    def install_metallb(
        self,
        release: ReleaseRef,
        config: LoadBalancerConfig,
        timeout: str,
    ) -> None:
        """Install MetalLB and store the pool needed to resume its configuration."""
        args = self._upgrade_install_args(
            release,
            self._config.metallb.source,
            self._config.metallb.version,
        )
        # Layer 2 announcement needs no BGP backend, which the chart otherwise
        # bundles as frr-k8s. The pool is stored under the key users set in
        # their values so a later install without values recovers it.
        args.extend(
            [
                "--set",
                "frrk8s.enabled=false",
                "--set-json",
                "loadBalancer.managedAddresses="
                + json.dumps(config.managed_addresses, separators=(",", ":")),
            ]
        )
        self._add_chart_image_sources(args, ("controller.image", "speaker.image"))
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
        # Materialize the proxy default through the upstream chart helper. Its
        # version is not the Gateway version and must remain owned by the chart.
        args.extend([
            "--set-string", "global.images.envoyProxy.image=docker.io/envoyproxy/envoy",
        ])
        images: dict[str, str] = {}
        for document in self._render_chart(args):
            if document["kind"] == "Deployment":
                containers = document["spec"]["template"]["spec"]["containers"]
                images["envoyGateway"] = containers[0]["image"]
            if (
                document["kind"] == "ConfigMap"
                and "envoy-gateway.yaml" in document.get("data", {})
            ):
                config = yaml.safe_load(document["data"]["envoy-gateway.yaml"])
                images["envoyProxy"] = _value_at(
                    config, "envoyProxy.provider.kubernetes.envoyDeployment.container.image"
                )
                images["ratelimit"] = _value_at(
                    config, "provider.kubernetes.rateLimitDeployment.container.image"
                )
        for role in ("envoyGateway", "envoyProxy", "ratelimit"):
            image = platform_image_reference(images[role], self._config.image_registry)
            args.extend(["--set-string", f"global.images.{role}.image={image}"])
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
        self._add_chart_image_sources(
            args,
            (
                "prometheusOperator.image",
                "prometheusOperator.admissionWebhooks.patch.image",
                "prometheusOperator.admissionWebhooks.deployment.image",
                "prometheusOperator.prometheusConfigReloader.image",
                "prometheusOperator.thanosImage",
                "prometheus.prometheusSpec.image",
                "alertmanager.alertmanagerSpec.image",
                "thanosRuler.thanosRulerSpec.image",
                "grafana.image",
                "grafana.sidecar.image",
                "grafana.initChownData.image",
                "grafana.downloadDashboardsImage",
                "grafana.testFramework.image",
                "kube-state-metrics.image",
                "prometheus-node-exporter.image",
            ),
            subcharts=("grafana", "kube-state-metrics", "prometheus-node-exporter"),
            version_prefixes=("kube-state-metrics.image", "prometheus-node-exporter.image"),
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
                # Receivers beside the managed Alertmanager route workload alerts;
                # configurations in other namespaces retain namespace isolation.
                "--set-string",
                "alertmanager.alertmanagerSpec.alertmanagerConfigMatcherStrategy.type="
                "OnNamespaceExceptForAlertmanagerNamespace",
                "--set-string",
                "grafana.sidecar.datasources.defaultDatasourceScrapeInterval=5s",
                "--set-json",
                "kube-state-metrics.metricLabelsAllowlist="
                + json.dumps(
                    [
                        "pods=[inference.foretoken.io/model-group,"
                        "inference.foretoken.io/model-role,"
                        "inference.foretoken.io/pd-pipeline-scope]"
                    ],
                    separators=(",", ":"),
                ),
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
            self._config.dcgm_exporter.version,
            timeout,
        )
        if reuse_values:
            args.append("--reuse-values")
        args.extend(
            [
                "--set",
                "serviceMonitor.enabled=true",
                "--set-string",
                "serviceMonitor.interval=5s",
                "--set-string",
                "serviceMonitor.scrapeTimeout=4s",
                "--set",
                "kubernetes.enablePodLabels=true",
                "--set-json",
                "kubernetes.podLabelAllowlistRegex=[\"^inference\\\\.foretoken\\\\.io/.*$\"]",
                "--set-string",
                "customMetrics=" + self._config.dcgm_metrics.replace(",", "\\,"),
                "--set-json",
                "securityContext.capabilities.add=[]",
            ]
        )
        if not reuse_values:
            self._add_chart_image_sources(args, ("image",))
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


def _value_at(values: dict[str, Any], path: str) -> Any:
    """Read a field whose shape is owned by the selected native chart."""
    value: Any = values
    for key in path.split("."):
        value = value[key]
    return value


def _merge_values(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Overlay chart mappings for image selection; Helm owns rendering and validation."""
    merged = defaults.copy()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def _image_repository_tag(reference: str) -> tuple[str, str]:
    """Split a source image reference, using Docker's implicit latest tag when omitted."""
    if ":" not in reference.rsplit("/", 1)[-1]:
        return reference, "latest"
    repository, separator, tag = reference.rpartition(":")
    if not separator or not repository or not tag:
        raise DeploymentError(f"source image must include a tag: {reference}")
    return repository, tag
