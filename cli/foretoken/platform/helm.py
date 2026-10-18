# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Helm chart adapters for CLI-managed Foretoken platform releases."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:
    from yaml import SafeLoader as _SafeLoader

from foretoken.accelerators.config import GPU_RESOURCE_BACKENDS
from foretoken.application_files import ApplicationFiles, application_references
from foretoken.command_execution import CommandExecution
from foretoken.kubernetes import Kubectl, resource_ref, timeout_seconds
from foretoken.manifest import DeploymentError, ResourceRef
from foretoken.network_sources import (
    select_github_download,
    select_platform_image_sources,
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
from foretoken.submission import _SERVICE_OPERATION_ANNOTATION

if TYPE_CHECKING:
    from foretoken.source import SourceImages


class Helm(HelmClient):
    """Build and execute Helm operations for platform-owned charts."""

    def _chart_source(self, source: str, version: str | None) -> str:
        """Resolve a managed chart source without overriding explicit registry choices."""
        if self._config.image_registry is not None:
            return source
        if not source.startswith("oci://"):
            return select_github_download(
                source, os.environ.get("FORETOKEN_GITHUB_MIRROR")
            )
        reference = f"{source}:{version}" if version is not None else source
        selected = select_platform_oci_reference(reference)
        return selected.removesuffix(f":{version}") if version is not None else selected

    @contextmanager
    def _prepared_chart(self, source: str, version: str | None) -> Iterator[Path]:
        """Keep one private chart copy alive through selection, rendering and installation."""
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

    @contextmanager
    def _deployment_chart(self, source_root: Path | None) -> Iterator[Path]:
        """Read a candidate chart or restore the installed platform's templates and effective values.

        Helm owns release storage and value merging. The installed path reads one
        revision through public commands, retaining unpublished source templates
        without consulting the current checkout or a chart registry. Callers copy
        the chart before modifying it.
        """
        if source_root is not None:
            yield source_root / "deploy/charts/foretoken"
            return
        release = self.platform_release()
        metadata = json.loads(self.run([
            "get", "metadata", release.name, "--namespace", release.namespace, "--output", "json",
        ]).stdout)
        selection = [release.name, "--namespace", release.namespace, "--revision", str(metadata["revision"])]
        # Foretoken's chart has no subcharts. Export its files as bytes rather
        # than implementing a serializer for Helm's in-memory Chart type.
        files_template = (
            '{{range .Release.Chart.Templates}}{{printf "%x\\t%x\\n" .Name .Data}}{{end}}'
            '{{range .Release.Chart.Files}}{{printf "%x\\t%x\\n" .Name .Data}}{{end}}'
            '{{if .Release.Chart.Schema}}{{printf "%x\\t%x\\n" "values.schema.json" .Release.Chart.Schema}}{{end}}'
        )
        metadata_template = (
            '{{with .Release.Chart.Metadata}}'
            '{{printf "apiVersion: %q\\nname: %q\\nversion: %q\\nappVersion: %q\\ntype: %q\\nkubeVersion: %q\\ndescription: %q\\n" '
            '.APIVersion .Name .Version .AppVersion .Type .KubeVersion .Description}}'
            '{{if .Annotations}}annotations:{{"\\n"}}'
            '{{range $key,$value := .Annotations}}{{printf "  %q: %q\\n" $key $value}}{{end}}{{end}}{{end}}'
        )
        with tempfile.TemporaryDirectory(prefix="foretoken-installed-chart-") as directory:
            chart = Path(directory)
            exported = self.run(["get", "all", *selection, "--template", files_template]).stdout
            for line in exported.splitlines():
                name, data = line.split("\t", 1)
                relative = Path(bytes.fromhex(name).decode())
                if relative.is_absolute() or ".." in relative.parts:
                    raise DeploymentError("installed chart contains an invalid file path")
                path = chart / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes.fromhex(data))
            (chart / "Chart.yaml").write_text(self.run([
                "get", "all", *selection, "--template", metadata_template,
            ]).stdout)
            (chart / "values.yaml").write_text(self.run([
                "get", "values", *selection, "--all", "--output", "json",
            ]).stdout)
            yield chart

    def _chart_image_defaults(
        self, chart: Path, subcharts: tuple[str, ...]
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
            child_metadata = yaml.safe_load(
                self.run(["show", "chart", str(dependency)]).stdout
            )
            values[name] = _merge_values(child, values.get(name, {}))
            versions[name] = str(child_metadata["appVersion"])
        return values, versions

    @staticmethod
    def _set_chart_defaults(chart: Path, updates: dict[str, Any]) -> None:
        """Keep automatic settings in chart defaults rather than stored user values."""
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

    def _set_chart_image_sources(
        self,
        chart: Path,
        paths: tuple[str, ...],
        *,
        release: ReleaseRef,
        overrides: dict[str, Any] | None = None,
        subcharts: tuple[str, ...] = (),
        version_prefixes: tuple[str, ...] = (),
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
                explicit = _value_at(overrides, path)
            except KeyError:
                explicit = {}
            if explicit is None or any(
                key in explicit for key in ("registry", "repository")
            ):
                continue
            image = _merge_values(_value_at(values, path), explicit)
            repository = image["repository"]
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
            images[path] = (reference, image)
        sources = select_platform_image_sources(
            {path: reference for path, (reference, _) in images.items()},
            self._config.image_registry,
            installed_images=self._installed_chart_images(release) if images else (),
        )
        updates = {}
        for path, (reference, image) in images.items():
            selected = sources[path]
            if selected != reference:
                repository = (
                    selected.rsplit("@", 1)[0]
                    if image.get("digest") or image.get("sha")
                    else _image_repository_tag(selected)[0]
                )
                if image.get("registry"):
                    registry, repository = repository.split("/", 1)
                    updates[f"{path}.registry"] = registry
                updates[f"{path}.repository"] = repository
        self._set_chart_defaults(chart, updates)

    def _render_chart(
        self, args: list[str], *, input_text: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Resolve image defaults through native templates with the install values."""
        command = ["template", args[2], args[3], "--dry-run=server"]
        for index, value in enumerate(args):
            if value in {
                "--namespace",
                "--values",
                "--set",
                "--set-string",
                "--set-json",
            }:
                command.extend([value, args[index + 1]])
        rendered = self.run(command, input_text=input_text).stdout
        return tuple(
            document
            for document in yaml.load_all(rendered, Loader=_SafeLoader)
            if document is not None
        )

    def _installed_chart_manifest(
        self, release: ReleaseRef
    ) -> tuple[dict[str, Any], ...]:
        """Read an installed release's rendered choices without freezing its chart defaults."""
        if not self.release_exists(release):
            return ()
        manifest = self.run(
            ["get", "manifest", release.name, "--namespace", release.namespace]
        ).stdout
        return tuple(
            document
            for document in yaml.load_all(manifest, Loader=_SafeLoader)
            if document is not None
        )

    def _installed_chart_images(self, release: ReleaseRef) -> set[str]:
        """Read prior images from native workloads and Prometheus Operator resources."""
        from foretoken.source_state import application_images

        documents = self._installed_chart_manifest(release)
        images = application_images(documents)
        # The operator creates these workloads from image references on its CRs.
        images.update(
            document["spec"]["image"]
            for document in documents
            if document["kind"] in {"Prometheus", "Alertmanager", "ThanosRuler"}
            and document["spec"].get("image")
        )
        return images

    def _stored_chart_values(self, release: ReleaseRef) -> dict[str, Any]:
        """Restore optional managed-chart customizations without freezing chart defaults."""
        return self.release_user_values(release) if self.release_exists(release) else {}

    def platform_release(self) -> ReleaseRef:
        """Return the single Foretoken platform release managed by the CLI."""
        return ReleaseRef(self._config.platform.release_name, self._config.namespace)

    def platform_configuration(self, kubectl: Kubectl | None = None) -> dict[str, Any]:
        """Read the installed platform identity and resolved defaults for deployment and source owners."""
        release = self.platform_release()
        configuration = (kubectl or Kubectl()).get_if_exists(
            "configmap", f"{release.name}-platform", release.namespace,
        )
        if configuration is None:
            raise DeploymentError("Foretoken platform is not installed; run foretoken install first")
        if not _owned_by_release(configuration, release):
            raise DeploymentError(f"Platform configuration is not owned by {release.display_name}")
        return configuration

    def deployment_values(self, kubectl: Kubectl | None = None) -> dict[str, Any]:
        """Read resolved runtime choices using the caller's observation deadline and cluster when supplied."""
        return json.loads(self.platform_configuration(kubectl)["data"]["deploymentValues"])

    def deployment_release(self, namespace: str) -> ReleaseRef:
        """Return the one serving-controller release owned by a deployment namespace."""
        if namespace == self._config.namespace:
            raise DeploymentError("Deploy services in a namespace separate from the Foretoken platform")
        return ReleaseRef(self._config.platform.release_name, namespace)

    def deployment_releases(self) -> tuple[ReleaseRef, ...]:
        """Find deployment releases, including failed submissions that still consume platform resources."""
        key, value = self._config.management_label
        # Same-named releases across namespaces have no stable offset-page order.
        listed = json.loads(self.run([
            "list", "--all-namespaces", "--max", "0", "--output", "json",
            "--selector", f"{key}={value},foretoken.io/scope=deployment",
        ]).stdout)
        return tuple(ReleaseRef(item["name"], item["namespace"]) for item in listed)

    def install_deployment(
        self, release: ReleaseRef, values: dict[str, Any], timeout: str, *, source_root: Path | None = None,
        operation: str = "",
    ) -> None:
        """Install namespace-local reconciliation from release defaults or a prepared candidate checkout.

        The submission owner supplies complete image and application selections, keeps
        services alive through finalization, and removes this release only after their deletion.
        """
        if self.release_exists(release) and not self.is_cli_managed(release):
            raise DeploymentError(f"Helm release {release.display_name} is not managed by foretoken")
        kubectl = Kubectl(deadline=time.monotonic() + timeout_seconds(timeout))
        for controller in kubectl.list_resources(
            ("deployments",), release.namespace,
            label_selector="app.kubernetes.io/name=foretoken-control-plane",
        ):
            if not _owned_by_release(controller, release):
                raise DeploymentError(
                    f"namespace {release.namespace} already has a Foretoken controller managed by another deployment"
                )
        source_mode = values["development"]["enabled"]
        configuration = self.platform_configuration(kubectl)
        with ExitStack() as charts:
            prepared = charts.enter_context(self._deployment_chart(source_root))
            # The platform owns registry credentials; this release owns the copies
            # needed by its controller.
            installed = json.loads(configuration["data"]["deploymentValues"])
            credentials = []
            pull_secrets = []
            for reference in installed["imagePullSecrets"]:
                secret = kubectl.get("secret", reference["name"], configuration["metadata"]["namespace"])
                name = f"{release.name}-pull-{secret['metadata']['uid']}"
                credentials.append({
                    "apiVersion": "v1", "kind": "Secret",
                    "metadata": {"name": name, "namespace": release.namespace},
                    "type": secret["type"], "data": secret["data"],
                })
                pull_secrets.append({"name": name})
            values = {**values, "imagePullSecrets": pull_secrets}
            labels = (
                ("foretoken.io/scope", "deployment"),
                (self._config.install_source_label, "source" if source_mode else "release"),
                *(((_SERVICE_OPERATION_ANNOTATION, operation),) if operation else ()),
            )
            args = self._upgrade_install_args(release, str(prepared), labels)
            # The submission owner already claimed the namespace; never recreate a retired one.
            args.remove("--create-namespace")
            args.extend(["--reset-values", "--values", "-", "--set-string", "scope=deployment"])
            input_text = yaml.safe_dump(values)
            # Platform defaults and source publications already resolve all images.
            desired = (*self._render_chart(args, input_text=input_text), *credentials)
            if self._deployment_matches(release, desired, kubectl):
                print(f"Reusing namespace controller: {release.display_name}", flush=True)
                return
            if credentials:
                prepared = charts.enter_context(self._prepared_chart(str(prepared), None))
                payload = yaml.safe_dump_all(credentials, sort_keys=False)
                (prepared / "templates" / "controller-pull-secrets.yaml").write_text(
                    "{{ " + json.dumps(payload) + " }}\n"
                )
                args[3] = str(prepared)
            args.append(f"--timeout={timeout}")
            self.run(args, input_text=input_text)

    def _deployment_matches(
        self, release: ReleaseRef, desired: tuple[dict[str, Any], ...], kubectl: Kubectl,
    ) -> bool:
        """Reuse an unchanged deployed release only while its live resources still match."""
        installed = self._installed_chart_manifest(release)
        if not installed or {resource_ref(obj): obj for obj in desired} != {
            resource_ref(obj): obj for obj in installed
        }:
            return False
        status = json.loads(self.run([
            "status", release.name, "--namespace", release.namespace, "--output", "json",
        ]).stdout)
        if status["info"]["status"] != "deployed":
            return False
        current = json.loads(kubectl.run(
            ["get", "--filename=-", "--ignore-not-found", "--output=json"],
            input_text=yaml.safe_dump_all(desired, sort_keys=False),
        ).stdout)
        objects = current["items"] if current["kind"] == "List" else [current]
        identities = {resource_ref(obj): obj for obj in objects}
        for document in desired:
            live = identities.get(resource_ref(document))
            if (
                live is None or live["metadata"].get("deletionTimestamp")
                or not _owned_by_release(live, release)
                or not _matches_declared_fields(live, document)
            ):
                return False
        return True

    def remove_deployment(self, release: ReleaseRef, timeout: str) -> None:
        """Remove a namespace controller after its submission owner has finalized all serving resources."""
        if self.release_exists(release):
            if not self.is_cli_managed(release) or not self.has_release_label(release, "foretoken.io/scope", "deployment"):
                raise DeploymentError(f"Helm release {release.display_name} is not a managed deployment")
            self.uninstall(release, timeout)

    def prometheus_release(self) -> ReleaseRef:
        """Return the Prometheus release managed with the platform."""
        return ReleaseRef(self._config.prometheus.release_name, self._config.namespace)

    def loki_release(self) -> ReleaseRef:
        """Return the platform log-storage release identity."""
        return ReleaseRef(self._config.loki.release_name, self._config.namespace)

    def log_collector_release(self) -> ReleaseRef:
        """Return the node log-collector release identity."""
        return ReleaseRef(
            self._config.log_collector.release_name, self._config.namespace
        )

    def install_loki(
        self, release: ReleaseRef, values: dict[str, Any], timeout: str
    ) -> None:
        """Install persistent Loki through its native chart and shared image selection."""
        # The StatefulSet template is immutable. Keep its creation size across upgrades;
        # the control plane grows live PVCs without changing that template.
        stored = self._stored_chart_values(release)
        if stored:
            values["singleBinary"]["persistence"]["size"] = stored["singleBinary"][
                "persistence"
            ]["size"]
        values = _merge_values(stored, values)
        chart = self._config.loki
        with self._prepared_chart(chart.source, chart.version) as prepared:
            args = self._managed_chart_args(release, str(prepared), timeout)
            self._set_chart_image_sources(
                prepared, ("loki.image",), release=release, overrides=values
            )
            args.extend(["--reset-values", "--values", "-"])
            self.run(args, input_text=yaml.safe_dump(values))

    def loki_statefulset(self, release: ReleaseRef) -> ResourceRef:
        """Read the installed log store identity for control-plane volume expansion."""
        return self._managed_chart_resource(
            release,
            api_version="apps/v1",
            kind="StatefulSet",
            chart_description="managed Loki chart",
        )

    def install_log_collector(
        self, release: ReleaseRef, values: dict[str, Any], timeout: str
    ) -> None:
        """Install node collectors without relocating their existing checkpoints and buffers."""
        chart = self._config.log_collector
        stored = self._stored_chart_values(release)
        # The tail database and unsent chunks must survive collector upgrades in place.
        for volume in stored.get("daemonSetVolumes", []):
            if volume["name"] == "state" and "hostPath" in volume:
                values["daemonSetVolumes"] = [
                    {**item, "hostPath": volume["hostPath"]} if item["name"] == "state" else item
                    for item in values["daemonSetVolumes"]
                ]
                break
        values = _merge_values(stored, values)
        with self._prepared_chart(chart.source, chart.version) as prepared:
            args = self._managed_chart_args(release, str(prepared), timeout)
            args.extend(["--reset-values", "--values", "-"])
            if "image" not in values:
                # cr.fluentbit.io fronts Docker Hub; select from its canonical publication.
                self._set_chart_defaults(
                    prepared, {"image.repository": "docker.io/fluent/fluent-bit"}
                )
                for document in self._render_chart(
                    args, input_text=yaml.safe_dump(values)
                ):
                    if document["kind"] == "DaemonSet":
                        image = document["spec"]["template"]["spec"]["containers"][0][
                            "image"
                        ]
                        selected = select_platform_image_sources(
                            {"image": image},
                            self._config.image_registry,
                            installed_images=self._installed_chart_images(release),
                        )["image"]
                        repository, tag = _image_repository_tag(selected)
                        self._set_chart_defaults(
                            prepared, {"image.repository": repository, "image.tag": tag}
                        )
            self.run(args, input_text=yaml.safe_dump(values))

    def _managed_chart_resource(
        self,
        release: ReleaseRef,
        *,
        api_version: str,
        kind: str,
        chart_description: str,
    ) -> ResourceRef:
        """Read one uniquely identified Kubernetes resource from a Helm manifest."""
        rendered = self.run(
            ["get", "manifest", release.name, "--namespace", release.namespace]
        ).stdout
        try:
            resources = [
                item
                for item in yaml.load_all(rendered, Loader=_SafeLoader)
                if isinstance(item, dict)
                and item.get("apiVersion") == api_version
                and item.get("kind") == kind
            ]
        except yaml.YAMLError as exc:
            raise DeploymentError(f"{chart_description} returned invalid YAML") from exc
        if len(resources) != 1:
            raise DeploymentError(f"{chart_description} must contain one {kind}")
        metadata = resources[0]["metadata"]
        return ResourceRef(
            kind,
            metadata["name"],
            metadata.get("namespace") or release.namespace,
        )

    def prometheus_resource(self, release: ReleaseRef) -> ResourceRef:
        """Read the Prometheus identity from the managed chart."""
        return self._managed_chart_resource(
            release,
            api_version="monitoring.coreos.com/v1",
            kind="Prometheus",
            chart_description="managed monitoring chart",
        )

    def dcgm_release(self) -> ReleaseRef:
        """Return the NVIDIA exporter release managed with the platform."""
        return ReleaseRef(
            self._config.dcgm_exporter.release_name, self._config.namespace
        )

    def dcgm_resource(self, release: ReleaseRef) -> ResourceRef:
        """Read the managed DCGM Exporter DaemonSet identity from Helm."""
        return self._managed_chart_resource(
            release,
            api_version="apps/v1",
            kind="DaemonSet",
            chart_description="managed DCGM Exporter chart",
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
        return ReleaseRef(
            self._config.leader_worker.release_name, self._config.namespace
        )

    def install_leader_worker(
        self,
        release: ReleaseRef,
        timeout: str,
        *,
        apply_crds: Callable[[str], None] | None = None,
    ) -> None:
        """Install the owned LWS release, updating CRDs from the same chart on upgrade."""
        chart = self._config.leader_worker
        values = self._stored_chart_values(release)
        with self._prepared_chart(chart.source, chart.version) as prepared:
            if apply_crds is not None:
                # Helm leaves CRDs unchanged on upgrade. The lifecycle applies
                # this chart's definitions through its existing cluster client.
                apply_crds(self.run(["show", "crds", str(prepared)]).stdout)
            args = self._upgrade_install_args(release, str(prepared))
            self._set_chart_image_sources(
                prepared, ("image.manager",), release=release, overrides=values
            )
            args.extend(["--reset-values", "--values", "-"])
            self._finish_upgrade(args, timeout)
            self.run(args, input_text=yaml.safe_dump(values))

    def dragonfly_release(self) -> ReleaseRef:
        """Return the optional file-distribution release owned by platform installation."""
        return ReleaseRef(self._config.dragonfly.release_name, self._config.namespace)

    def dragonfly_daemonset(self, release: ReleaseRef) -> ResourceRef:
        """Read the actual node client identity from the selected Dragonfly release."""
        return self._managed_chart_resource(
            release,
            api_version="apps/v1",
            kind="DaemonSet",
            chart_description="Dragonfly chart",
        )

    def install_dragonfly(
        self,
        release: ReleaseRef,
        accelerator_resource: str,
        image_pull_secrets: tuple[dict[str, str], ...],
        timeout: str,
    ) -> None:
        """Install standalone scheduling and node peers without databases or runtime rewrites."""
        chart = self._config.dragonfly
        values = self._stored_chart_values(release)
        args = self._upgrade_install_args(release, chart.source)
        args.extend(
            [
                "--set",
                "manager.enable=false",
                "--set",
                "mysql.enable=false",
                "--set",
                "redis.enable=false",
                "--set",
                "seedClient.enable=false",
                "--set",
                "client.hostNetwork=false",
                "--set",
                "client.hostPID=false",
                "--set",
                "client.hostIPC=false",
                "--set",
                "client.dfinit.enable=false",
                "--set-json",
                "global.imagePullSecrets=" + json.dumps(image_pull_secrets),
            ]
        )
        if accelerator_resource:
            args.extend(
                [
                    "--set-json",
                    "client.tolerations="
                    + json.dumps(
                        [
                            {
                                "key": accelerator_resource,
                                "operator": "Exists",
                                "effect": "NoSchedule",
                            },
                        ]
                    ),
                ]
            )
        with self._prepared_chart(chart.source, chart.version) as prepared:
            args[3] = str(prepared)
            self._set_chart_image_sources(
                prepared,
                ("scheduler.image", "client.image", "client.initContainer.image"),
                release=release,
                overrides=values,
            )
            args.extend(["--reset-values", "--values", "-"])
            self._finish_upgrade(args, timeout)
            self.run(args, input_text=yaml.safe_dump(values))

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

    def _upgrade_install_args(
        self,
        release: ReleaseRef,
        chart: str,
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
        return args

    @staticmethod
    def _finish_upgrade(args: list[str], timeout: str) -> None:
        """Wait for one managed Helm upgrade to finish."""
        args.extend(["--wait", f"--timeout={timeout}"])

    def _managed_chart_args(
        self,
        release: ReleaseRef,
        chart: str,
        timeout: str,
    ) -> list[str]:
        """Build one managed chart upgrade and wait configuration."""
        args = self._upgrade_install_args(release, chart)
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
        if gateway_section_name or (frontend_mode == "gateway" and gateway_name):
            args.extend(
                [
                    "--set-string",
                    f"frontend.gateway.sectionName={gateway_section_name}",
                ]
            )

    @staticmethod
    def _platform_images(
        documents: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, str], str | None]:
        """Read platform image references and the selected backend from rendered Helm resources."""
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
                and document["metadata"]
                .get("labels", {})
                .get("foretoken.io/profile-viewer")
                == "configuration"
                and document["data"]["nsightImage"]
            ):
                for key, path in (
                    ("nsightImage", "profiling.nsightViewerImage"),
                    ("proxyImage", "profiling.viewerProxyImage"),
                ):
                    if reference := document["data"][key]:
                        images[path] = reference
            if document["kind"] not in {"Deployment", "DaemonSet", "StatefulSet"}:
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                if (
                    container["name"] == "files"
                    and document["metadata"]
                    .get("labels", {})
                    .get("foretoken.io/application-files")
                    == "server"
                ):
                    images["applicationFiles.serverImage"] = container["image"]
                if (
                    container["name"] == "valkey"
                    and document["metadata"]
                    .get("labels", {})
                    .get("app.kubernetes.io/name")
                    == "foretoken-admission-store"
                ):
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
                            runtime_backend = GPU_RESOURCE_BACKENDS.get(
                                argument.removeprefix("--gpu-resource-name=")
                            )
                        for prefix, path in (
                            ("--frontend-image=", "frontend.image"),
                            ("--inference-engine-image=", "runtime.vllm.image"),
                        ):
                            if argument.startswith(prefix):
                                images[path] = argument.removeprefix(prefix)
        return images, runtime_backend

    def _set_platform_image_sources(
        self,
        args: list[str],
        overrides: dict[str, Any],
        source_mode: bool,
        input_text: str | None,
    ) -> None:
        """Resolve platform image defaults, preserving explicit and installed sources."""
        images, runtime_backend = self._platform_images(
            self._render_chart(args, input_text=input_text)
        )
        previous, _ = self._platform_images(
            self._installed_chart_manifest(self.platform_release())
        )
        references = {}
        prior_sources = {}
        for path, reference in images.items():
            if source_mode and path in {
                "image.repository",
                "frontend.image",
                "runtime.vllm.image",
            }:
                continue
            try:
                explicit = _value_at(overrides, path)
            except KeyError:
                explicit = None
            if explicit is not None and explicit != "auto":
                continue
            prior = previous.get(path)
            if path == "runtime.vllm.image":
                # Preserve the chart's auto/backend selection and its user-supplied alternatives.
                if runtime_backend is None:
                    continue
                path = f"runtime.vllm.environmentImages.{runtime_backend}"
                try:
                    if _value_at(overrides, path):
                        continue
                except KeyError:
                    pass
            references[path] = reference
            if prior is not None:
                prior_sources[path] = prior
        sources = select_platform_image_sources(
            references, self._config.image_registry, previous=prior_sources
        )
        updates = {}
        for path, selected in sources.items():
            if selected == references[path]:
                continue
            if path == "image.repository":
                selected = (
                    selected.split("@", 1)[0]
                    if "@" in selected
                    else _image_repository_tag(selected)[0]
                )
            updates[path] = selected
        self._set_chart_defaults(Path(args[3]), updates)

    def prepare_source_origin(
        self, root: Path, values: tuple[dict[str, Any], ...], timeout: str
    ) -> ApplicationFiles:
        """Prepare chart-owned storage and return its ready source-build publisher location."""
        release = self.platform_release()
        args = self._upgrade_install_args(
            release, str(root / "deploy/charts/foretoken")
        )
        merged: dict[str, Any] = {}
        for value in values:
            merged = _merge_values(merged, value)
        args.extend(
            [
                "--values",
                "-",
                "--set",
                "development.enabled=true",
                "--set-string",
                "observability.mode=disabled",
                "--set-string",
                "applicationFiles.releaseURL=",
            ]
        )
        input_text = yaml.safe_dump(merged)
        with self._prepared_chart(args[3], None) as chart:
            args[3] = str(chart)
            self._set_platform_image_sources(args, merged, True, input_text)
            with self._prepare_application_origin(args, input_text, timeout) as origin:
                # Source charts always render storage through development.enabled.
                assert origin is not None
                return origin

    @contextmanager
    def _prepare_application_origin(
        self, args: list[str], input_text: str | None, timeout: str
    ) -> Iterator[ApplicationFiles | None]:
        """Bootstrap native storage and hold release publication ownership through Helm selection."""
        release = self.platform_release()
        rendered = self._render_chart(args, input_text=input_text)
        documents = [
            document
            for document in rendered
            if document["metadata"].get("labels", {}).get("app.kubernetes.io/name")
            == "foretoken-application-files"
        ]
        if not documents:
            yield
            return
        kubectl = Kubectl()
        if not kubectl.exists("namespace", release.namespace):
            kubectl.run(["create", "namespace", release.namespace])
        operations = []
        for document in documents:
            metadata = document["metadata"]
            current = kubectl.get_if_exists(
                document["kind"], metadata["name"], release.namespace
            )
            if current is not None:
                if not _owned_by_release(current, release):
                    raise DeploymentError(
                        f"{document['kind']}/{metadata['name']} is not owned by Helm release "
                        f"{release.display_name}"
                    )
                metadata["resourceVersion"] = current["metadata"]["resourceVersion"]
            metadata.setdefault("annotations", {}).update(
                {
                    "meta.helm.sh/release-name": release.name,
                    "meta.helm.sh/release-namespace": release.namespace,
                }
            )
            operations.append(("create" if current is None else "apply", document))
        # Check every existing resource before writing; create and resourceVersion
        # preconditions prevent taking over a concurrently replaced object.
        for operation, document in operations:
            kubectl.run([operation, "-f", "-"], input_text=yaml.safe_dump(document))
        origin = ApplicationFiles(kubectl, release.namespace)
        origin.prepare(timeout)
        configuration = next(
            document["data"]
            for document in documents
            if document["kind"] == "ConfigMap"
        )
        if not configuration.get("releaseURL"):
            yield origin
            return

        def retained() -> set[str] | None:
            """Read live consumers and Helm history only after release Job ownership."""
            references = origin.references(self.application_history())
            if references is not None:
                references.update(application_references(rendered))
                references.add(configuration["revision"])
            return references

        def previous() -> dict[str, str]:
            """Find installed component directories for immutable-file reuse."""
            if not self.release_exists(release):
                return {}
            manifest = self.run(
                [
                    "get",
                    "manifest",
                    release.name,
                    "--namespace",
                    release.namespace,
                ]
            ).stdout
            references = application_references(
                document
                for document in yaml.load_all(manifest, Loader=_SafeLoader)
                if document is not None
            )
            prefix = origin.endpoint.rstrip("/") + "/"
            return {
                component: revision
                for reference in sorted(references)
                if reference.startswith(prefix)
                for component, revision in [
                    reference.removeprefix(prefix).rstrip("/").split("/", 1)
                ]
            }

        with origin.import_release(
            configuration["releaseURL"],
            configuration["revision"],
            retained,
            previous,
            timeout=timeout,
            credentials_secret=configuration["credentialsSecret"],
        ):
            yield origin

    def application_origin_resources(self) -> tuple[dict[str, Any], ...]:
        """Find native file-origin resources owned by this release, including failed bootstraps."""
        release = self.platform_release()
        kubectl = Kubectl()
        if not kubectl.exists("namespace", release.namespace):
            return ()
        return tuple(
            document
            for document in kubectl.list_resources(
                ("persistentvolumeclaims", "configmaps", "deployments", "services"),
                release.namespace,
                label_selector="app.kubernetes.io/name=foretoken-application-files",
            )
            if _owned_by_release(document, release)
        )

    def remove_application_origin(
        self, resources: tuple[dict[str, Any], ...], timeout: str
    ) -> None:
        """Remove an unadopted file origin and its PVC-owned writers during explicit uninstall."""
        if not resources:
            return
        kubectl = Kubectl()
        release = self.platform_release()
        claims = {
            document["metadata"]["uid"]
            for document in resources
            if document["kind"] == "PersistentVolumeClaim"
        }
        for job in kubectl.list_resources(
            ("jobs",),
            release.namespace,
            label_selector="foretoken.io/application-files=publisher",
        ):
            if any(
                owner["kind"] == "PersistentVolumeClaim" and owner["uid"] in claims
                for owner in job["metadata"].get("ownerReferences", [])
            ):
                kubectl.run(
                    [
                        "delete",
                        "job",
                        job["metadata"]["name"],
                        "-n",
                        release.namespace,
                        "--cascade=foreground",
                        "--wait=true",
                        "--timeout=" + timeout,
                    ]
                )
        kubectl.delete(yaml.safe_dump_all(resources), timeout)

    def stop_platform_tasks(self, kubectl: Kubectl, timeout: str) -> None:
        """Stop native maintenance and failed bootstrap jobs before retiring their shared files and storage."""
        release = self.platform_release()
        if not kubectl.exists("namespace", release.namespace):
            return
        for resource in kubectl.list_resources(
            ("cronjobs", "jobs"), release.namespace, label_selector="foretoken.io/platform-task=true",
        ):
            if _owned_by_release(resource, release):
                kubectl.run([
                    "delete", resource["kind"], resource["metadata"]["name"],
                    "--namespace", release.namespace, "--cascade=foreground",
                    "--wait=true", f"--timeout={timeout}",
                ])

    def admission_store_claims(self, kubectl: Kubectl) -> tuple[dict[str, Any], ...]:
        """Find verified cleanup claims, including after the platform release was removed."""
        release = self.platform_release()
        if not kubectl.exists("namespace", release.namespace):
            return ()
        return tuple(
            claim["metadata"]
            for claim in kubectl.list_resources(
                ("persistentvolumeclaims",),
                release.namespace,
                label_selector="app.kubernetes.io/name=foretoken-admission-store",
            )
            if _owned_by_release(claim, release)
        )

    def prepare_admission_store_uninstall(
        self, kubectl: Kubectl, timeout: str
    ) -> tuple[dict[str, Any], ...]:
        """Stop owned stores and identify PVC UIDs for native cleanup after Helm uninstall."""
        release = self.platform_release()
        claims_to_wait = []
        for store in kubectl.list_resources(
            ("statefulsets",),
            release.namespace,
            label_selector="app.kubernetes.io/name=foretoken-admission-store",
        ):
            if not _owned_by_release(store, release):
                continue
            metadata = store["metadata"]
            name, uid = metadata["name"], metadata["uid"]
            labels = store["spec"]["selector"]["matchLabels"]
            selector = ",".join(f"{key}={value}" for key, value in labels.items())

            def controlled(document: dict[str, Any], uid: str = uid) -> bool:
                return any(
                    owner.get("controller") and owner["uid"] == uid
                    for owner in document["metadata"].get("ownerReferences", [])
                )

            pods = [
                pod
                for pod in kubectl.list_resources(
                    ("pods",), release.namespace, label_selector=selector
                )
                if controlled(pod)
            ]
            templates = {
                template["metadata"]["name"]
                for template in store["spec"].get("volumeClaimTemplates", [])
            }
            mounted = {
                volume["persistentVolumeClaim"]["claimName"]
                for pod in pods
                for volume in pod["spec"].get("volumes", [])
                if volume["name"] in templates and "persistentVolumeClaim" in volume
            }
            owners = {uid, *(pod["metadata"]["uid"] for pod in pods)}

            def validate_claim(
                claim: dict[str, Any],
                labels: dict[str, str] = labels,
                owners: set[str] = owners,
            ) -> None:
                """Reject conflicting ownership before changing a store or marking its claim."""
                info = claim["metadata"]
                annotations = info.get("annotations", {})
                if (
                    (
                        not controlled(claim)
                        and any(
                            info.get("labels", {}).get(key) != value
                            for key, value in labels.items()
                        )
                    )
                    or info.get("labels", {}).get(
                        "app.kubernetes.io/managed-by", "Helm"
                    )
                    != "Helm"
                    or any(
                        owner["uid"] not in owners
                        for owner in info.get("ownerReferences", [])
                    )
                    or any(
                        annotations.get(key, expected) != expected
                        for key, expected in (
                            ("meta.helm.sh/release-name", release.name),
                            ("meta.helm.sh/release-namespace", release.namespace),
                        )
                    )
                ):
                    raise DeploymentError(
                        f"admission store PVC {release.namespace}/{info['name']} has another owner"
                    )

            claims = []
            for claim in kubectl.list_resources(
                ("persistentvolumeclaims",), release.namespace
            ):
                if not controlled(claim) and claim["metadata"]["name"] not in mounted:
                    continue
                validate_claim(claim)
                claims.append(claim)

            # Scaling down lets the StatefulSet controller update legacy claim ownership
            # before deleting even an unhealthy Pod. PVC deletion remains native GC's job.
            policy = {"whenDeleted": "Delete", "whenScaled": "Retain"}
            kubectl.run(
                [
                    "patch",
                    "statefulset",
                    name,
                    "-n",
                    release.namespace,
                    "--type=merge",
                    "--patch",
                    json.dumps(
                        {
                            "metadata": {
                                "resourceVersion": metadata["resourceVersion"]
                            },
                            "spec": {
                                "replicas": 0,
                                "persistentVolumeClaimRetentionPolicy": policy,
                            },
                        }
                    ),
                ]
            )
            deadline = time.monotonic() + timeout_seconds(timeout)
            while True:
                current = kubectl.get_if_exists("statefulset", name, release.namespace)
                if (
                    current is None
                    or current["metadata"]["uid"] != uid
                    or current["spec"].get("replicas") != 0
                    or current["spec"].get("persistentVolumeClaimRetentionPolicy")
                    != policy
                ):
                    raise DeploymentError(
                        f"admission store {release.namespace}/{name} changed during uninstall"
                    )
                stopped = not any(
                    controlled(pod)
                    for pod in kubectl.list_resources(
                        ("pods",), release.namespace, label_selector=selector
                    )
                )
                owned = True
                for claim in claims:
                    info = claim["metadata"]
                    current_claim = kubectl.get_if_exists(
                        "persistentvolumeclaim", info["name"], release.namespace
                    )
                    if (
                        current_claim is not None
                        and current_claim["metadata"]["uid"] == info["uid"]
                        and not controlled(current_claim)
                    ):
                        owned = False
                if stopped and owned:
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"admission store {release.namespace}/{name} did not finish stopping and assigning PVC ownership within {timeout}"
                    )
                time.sleep(1)
            # Persist verified release ownership before removing the StatefulSet so a
            # retry can still wait for its claims after Helm has removed the release.
            for claim in claims:
                info = claim["metadata"]
                current = kubectl.get_if_exists(
                    "persistentvolumeclaim", info["name"], release.namespace
                )
                if current is None or current["metadata"]["uid"] != info["uid"]:
                    continue
                if not controlled(current):
                    raise DeploymentError(
                        f"PVC {release.namespace}/{info['name']} changed ownership during uninstall"
                    )
                validate_claim(current)
                kubectl.run(
                    [
                        "patch",
                        "persistentvolumeclaim",
                        info["name"],
                        "-n",
                        release.namespace,
                        "--type=merge",
                        "--patch",
                        json.dumps(
                            {
                                "metadata": {
                                    "resourceVersion": current["metadata"][
                                        "resourceVersion"
                                    ],
                                    "labels": {
                                        **labels,
                                        "app.kubernetes.io/managed-by": "Helm",
                                    },
                                    "annotations": {
                                        "meta.helm.sh/release-name": release.name,
                                        "meta.helm.sh/release-namespace": release.namespace,
                                    },
                                }
                            }
                        ),
                    ]
                )
                claims_to_wait.append(info)
        return tuple(claims_to_wait)

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
        dragonfly_socket_path: str,
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
            (
                (
                    self._config.install_source_label,
                    "source" if source_mode else "release",
                ),
                ("foretoken.io/scope", "platform"),
            ),
        )
        args.extend(["--set-string", "scope=platform"])
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
        args.extend(["--set", f"development.enabled={str(source_mode).lower()}"])
        args.extend(
            ["--set-string", f"observability.prometheus={observability_prometheus}"]
        )
        args.extend(
            ["--set-string", f"observability.logs.datasourceURL={log_endpoint}"]
        )
        args.extend(
            [
                "--set-string",
                f"observability.logs.managedStatefulSet={log_storage_statefulset}",
            ]
        )
        if grafana_anonymous_access is not None:
            args.extend(
                [
                    "--set-json",
                    "observability.grafana.anonymousAccess="
                    + json.dumps(grafana_anonymous_access),
                ]
            )
        if gpu_resource_name is not None:
            args.extend(
                [
                    "--set-string",
                    f"runtime.vllm.gpu.resourceName={gpu_resource_name}",
                ]
            )
        args.extend(
            [
                "--set-string",
                f"modelDistribution.dragonfly.socketPath={dragonfly_socket_path}",
            ]
        )
        if rdma_managed:
            args.extend(
                [
                    "--set",
                    "rdma.managed=true",
                    "--set-json",
                    "rdma.nodeNames=" + json.dumps(rdma_node_names),
                ]
            )
        if rdma_resource_name is not None:
            args.extend(["--set-string", f"rdma.resourceName={rdma_resource_name}"])
        # Individual values retain their own source registry and explicit overrides.
        # A blanket registry override would also rewrite user and source images.
        args.extend(["--set-string", "global.imageRegistry="])
        if source_images is not None:
            control_plane_image = source_images.control_plane
            frontend_image = source_images.frontend
            model_server_image = source_images.model_server
            repository, tag = _image_repository_tag(control_plane_image)
            args.extend(
                [
                    "--set-string",
                    "controller.applicationURL="
                    + source_images.applications["control-plane"],
                    "--set-string",
                    "frontend.applicationURL=" + source_images.applications["frontend"],
                    "--set-string",
                    "runtime.vllm.applicationURL="
                    + source_images.applications["model-server"],
                    "--set-string",
                    "applicationFiles.releaseURL=",
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
        input_text = (
            yaml.safe_dump(stored_values) if stored_values is not None else None
        )
        overrides = stored_values or {}
        for value in load_platform_values(values):
            overrides = _merge_values(overrides, value)
        with self._prepared_chart(chart, chart_version) as prepared:
            args[3] = str(prepared)
            self._set_platform_image_sources(args, overrides, source_mode, input_text)
            with self._prepare_application_origin(args, input_text, timeout):
                self._finish_upgrade(args, timeout)
                self.run(args, input_text=input_text)
                self.platform_configuration()

    def wait_control_plane(self, release: ReleaseRef, timeout: str) -> str:
        """Wait for leadership and return the namespace controller's UID/generation selection."""
        kubectl = Kubectl()
        deadline = time.monotonic() + timeout_seconds(timeout)

        def run(arguments: list[str]) -> str:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeploymentError(f"Timed out waiting for {release.display_name} controller leadership")
            return kubectl.run(arguments, timeout=remaining).stdout

        selector = ",".join(f"{key}={value}" for key, value in self.platform_selector_labels)
        deployments = json.loads(run(["get", "deployments", "-n", release.namespace, "-l", selector, "-o", "json"]))["items"]
        if len(deployments) != 1:
            raise DeploymentError(f"Expected one controller Deployment for {release.display_name}")
        name = deployments[0]["metadata"]["name"]
        run(["rollout", "status", "deployment/" + name, "-n", release.namespace, "--timeout=" + timeout])
        deployment = json.loads(run(["get", "deployment", name, "-n", release.namespace, "-o", "json"]))
        selection = f"{deployment['metadata']['uid']}:{deployment['metadata']['generation']}"
        manager = next(container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "manager")
        if not any(argument in {"--leader-elect", "--leader-elect=true"} for argument in manager.get("args", [])):
            return selection
        selector = ",".join(f"{key}={value}" for key, value in deployment["spec"]["selector"]["matchLabels"].items())
        observed = None
        while time.monotonic() < deadline:
            pods = json.loads(run(["get", "pods", "-n", release.namespace, "-l", selector, "-o", "json"]))["items"]
            active = {
                pod["metadata"]["name"]: status["containerID"]
                for pod in pods if not pod["metadata"].get("deletionTimestamp")
                for status in pod.get("status", {}).get("containerStatuses", [])
                if status["name"] == "manager" and status.get("ready")
                and "running" in status["state"] and status.get("containerID")
            }
            raw = run(["get", "lease", "inference.foretoken.io", "-n", release.namespace, "-o", "json", "--ignore-not-found"])
            lease = json.loads(raw).get("spec", {}) if raw.strip() else {}
            holder = lease.get("holderIdentity", "")
            pod_name = holder.rsplit("_", 1)[0]
            renewal = lease.get("renewTime")
            # A new renewal from the same running container excludes a retired
            # process in a restarted Pod without relying on workstation clock time.
            current = (holder, active.get(pod_name), renewal)
            if current[1] and renewal:
                if observed is not None and observed[:2] == current[:2] and observed[2] != renewal:
                    return selection
                observed = current
            else:
                observed = None
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise DeploymentError(f"Timed out waiting for {release.display_name} controller leadership")

    def application_history(self) -> set[str] | None:
        """Retain application publications selected by available Helm revisions."""
        return self._history_references(application_references)

    def image_history(self) -> set[str] | None:
        """Retain images selected by installed defaults or an available Helm rollback."""
        from foretoken.source_state import application_images

        return self._history_references(application_images)

    def _history_references(self, extract: Callable[[Iterable[dict[str, Any]]], set[str]]) -> set[str] | None:
        """Retain shared defaults and every namespace controller's available rollback selections."""
        platform = self.platform_release()
        releases = (*((platform,) if self.release_exists(platform) else ()), *self.deployment_releases())
        references = set()
        for release in releases:
            retained = self._release_history_references(release, extract)
            if retained is None:
                return None
            references.update(retained)
        return references

    def _release_history_references(
        self, release: ReleaseRef, extract: Callable[[Iterable[dict[str, Any]]], set[str]],
    ) -> set[str] | None:
        """Read one release's rollback history, deferring collection when its revision advances."""
        status = json.loads(
            self.run(
                [
                    "status",
                    release.name,
                    "--namespace",
                    release.namespace,
                    "--output",
                    "json",
                ]
            ).stdout
        )
        revision = int(status["version"])
        history = json.loads(
            self.run(
                [
                    "history",
                    release.name,
                    "--namespace",
                    release.namespace,
                    "--max",
                    str(revision),
                    "--output",
                    "json",
                ]
            ).stdout
        )
        if any(int(item["revision"]) > revision for item in history):
            return None
        references = set()
        for item in history:
            manifest = self.run(
                [
                    "get",
                    "manifest",
                    release.name,
                    "--namespace",
                    release.namespace,
                    "--revision",
                    str(item["revision"]),
                ]
            ).stdout
            references.update(
                extract(
                    document
                    for document in yaml.load_all(manifest, Loader=_SafeLoader)
                    if document is not None
                )
            )
        return references

    def install_metallb(
        self,
        release: ReleaseRef,
        config: LoadBalancerConfig,
        timeout: str,
    ) -> None:
        """Install MetalLB and store the pool needed to resume its configuration."""
        values = self._stored_chart_values(release)
        args = self._upgrade_install_args(
            release,
            self._config.metallb.source,
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
        with self._prepared_chart(
            self._config.metallb.source, self._config.metallb.version
        ) as chart:
            args[3] = str(chart)
            self._set_chart_image_sources(
                chart,
                ("controller.image", "speaker.image"),
                release=release,
                overrides=values,
            )
            args.extend(["--reset-values", "--values", "-"])
            self._finish_upgrade(args, timeout)
            self.run(args, input_text=yaml.safe_dump(values))

    @staticmethod
    def _envoy_gateway_images(documents: tuple[dict[str, Any], ...]) -> dict[str, str]:
        """Read rendered controller and proxy image choices from an Envoy Gateway release."""
        images = {}
        for document in documents:
            if document["kind"] == "Deployment":
                containers = document["spec"]["template"]["spec"]["containers"]
                images["envoyGateway"] = containers[0]["image"]
            if document["kind"] == "ConfigMap" and "envoy-gateway.yaml" in document.get(
                "data", {}
            ):
                config = yaml.safe_load(document["data"]["envoy-gateway.yaml"])
                for role, path in (
                    (
                        "envoyProxy",
                        "envoyProxy.provider.kubernetes.envoyDeployment.container.image",
                    ),
                    (
                        "ratelimit",
                        "provider.kubernetes.rateLimitDeployment.container.image",
                    ),
                ):
                    images[role] = _value_at(config, path)
        return images

    def install_envoy_gateway(
        self,
        release: ReleaseRef,
        timeout: str,
    ) -> None:
        """Install or update the CLI-managed Envoy Gateway release."""
        args = self._managed_chart_args(
            release,
            self._config.envoy_gateway.source,
            timeout,
        )
        args.extend(
            [
                "--set-string",
                "config.envoyGateway.gateway.controllerName="
                + self._config.envoy_gateway_controller,
            ]
        )
        values = self._stored_chart_values(release)
        input_text = yaml.safe_dump(values)
        args.extend(["--reset-values", "--values", "-"])
        with self._prepared_chart(
            self._config.envoy_gateway.source, self._config.envoy_gateway.version
        ) as chart:
            args[3] = str(chart)
            # The chart helper owns the proxy version independently of the Gateway version.
            self._set_chart_defaults(
                chart, {"global.images.envoyProxy.image": "docker.io/envoyproxy/envoy"}
            )
            images = self._envoy_gateway_images(
                self._render_chart(args, input_text=input_text)
            )
            previous = self._envoy_gateway_images(
                self._installed_chart_manifest(release)
            )
            references = {}
            prior_sources = {}
            for role in ("envoyGateway", "envoyProxy", "ratelimit"):
                path = f"global.images.{role}.image"
                try:
                    if _value_at(values, path):
                        continue
                except KeyError:
                    pass
                references[path] = images[role]
                if role in previous:
                    prior_sources[path] = previous[role]
            sources = select_platform_image_sources(
                references, self._config.image_registry, previous=prior_sources
            )
            self._set_chart_defaults(chart, sources)
            self.run(args, input_text=input_text)

    def install_prometheus(
        self,
        release: ReleaseRef,
        service_monitor_namespaces: tuple[str, ...],
        reuse_values: bool,
        timeout: str,
        *,
        anonymous_access: bool | None = None,
    ) -> None:
        """Install managed monitoring with stored settings and the platform's Grafana access choice."""
        # Preserve monitoring customizations; the platform access choice grants only viewing.
        anonymous_settings = {"enabled": True, "org_role": "Viewer"}
        values = {
            "grafana": {
                "defaultDashboardsEnabled": False,
                "grafana.ini": {
                    "auth.anonymous": anonymous_settings,
                    "users": {"default_theme": "light"},
                    # Bundled plugins follow image upgrades instead of live replacement.
                    "plugins": {"preinstall_auto_update": False},
                },
            },
        }
        if reuse_values:
            values = _merge_values(values, self.release_user_values(release))
        if anonymous_access is not None:
            values = _merge_values(
                values,
                {
                    "grafana": {
                        "grafana.ini": {
                            "auth.anonymous": {
                                **anonymous_settings,
                                "enabled": anonymous_access,
                            }
                        }
                    }
                },
            )
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
                "prometheus.prometheusSpec.ruleNamespaceSelector={}",
                # Receivers beside the managed Alertmanager route workload alerts;
                # configurations in other namespaces retain namespace isolation.
                "--set-string",
                (
                    "alertmanager.alertmanagerSpec.alertmanagerConfigMatcherStrategy.type="
                    "OnNamespaceExceptForAlertmanagerNamespace"
                ),
                "--set-string",
                "grafana.sidecar.datasources.defaultDatasourceScrapeInterval=5s",
                "--set-json",
                "kube-state-metrics.metricLabelsAllowlist="
                + json.dumps(
                    [
                        (
                            "pods=[inference.foretoken.io/model-group,"
                            "inference.foretoken.io/model-role,"
                            "inference.foretoken.io/pd-pipeline-scope]"
                        )
                    ],
                    separators=(",", ":"),
                ),
            ]
        )
        args.extend(["--reset-values", "--values", "-"])
        with self._prepared_chart(
            self._config.prometheus.source, self._config.prometheus.version
        ) as chart:
            args[3] = str(chart)
            # Retain CPU quota and throttled-time counters in the upstream defaults.
            # Explicit administrator relabelings still override this list through Helm.
            defaults = yaml.safe_load((chart / "values.yaml").read_text())
            rules = defaults["kubelet"]["serviceMonitor"]["cAdvisorMetricRelabelings"]
            for rule in rules:
                if rule.get("sourceLabels") != ["__name__"] or rule.get("action") != "drop":
                    continue
                expression = rule.get("regex", "")
                if expression == "container_spec.*":
                    rule["regex"] = "container_spec_(cpu_shares|memory_.*)"
                elif expression.startswith("container_cpu_("):
                    rule["regex"] = expression.replace("cfs_throttled_seconds_total|", "")
            self._set_chart_defaults(
                chart, {"kubelet.serviceMonitor.cAdvisorMetricRelabelings": rules}
            )
            self._set_chart_image_sources(
                chart,
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
                release=release,
                overrides=values,
                subcharts=("grafana", "kube-state-metrics", "prometheus-node-exporter"),
                version_prefixes=(
                    "kube-state-metrics.image",
                    "prometheus-node-exporter.image",
                ),
            )
            self.run(args, input_text=yaml.safe_dump(values))

    def install_dcgm_exporter(
        self,
        release: ReleaseRef,
        observability_labels: tuple[tuple[str, str], ...],
        node_selector: tuple[str, str] | None,
        reuse_values: bool,
        timeout: str,
        *,
        visible_devices: str | None,
    ) -> None:
        """Install or upgrade the CLI-managed NVIDIA DCGM Exporter release."""
        args = self._managed_chart_args(
            release,
            self._config.dcgm_exporter.source,
            timeout,
        )
        stored = self.release_user_values(release) if reuse_values else {}
        if reuse_values:
            args.extend(["--reset-values", "--values", "-"])
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
                'kubernetes.podLabelAllowlistRegex=["^inference\\\\.foretoken\\\\.io/.*$"]',
                "--set-string",
                "customMetrics=" + self._config.dcgm_metrics.replace(",", "\\,"),
                "--set-json",
                "securityContext.capabilities.add=[]",
            ]
        )
        if visible_devices is not None:
            environment = [
                entry
                for entry in stored.get("extraEnv") or []
                if entry["name"] != "NVIDIA_VISIBLE_DEVICES"
            ]
            environment.append(
                {"name": "NVIDIA_VISIBLE_DEVICES", "value": visible_devices}
            )
            args.extend(["--set-json", "extraEnv=" + json.dumps(environment)])
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
        with self._prepared_chart(
            self._config.dcgm_exporter.source, self._config.dcgm_exporter.version
        ) as chart:
            args[3] = str(chart)
            self._set_chart_image_sources(
                chart, ("image",), release=release, overrides=stored
            )
            self.run(args, input_text=yaml.safe_dump(stored) if reuse_values else None)


def _matches_declared_fields(actual: Any, desired: Any) -> bool:
    """Compare Helm-owned configuration while allowing API defaults and unrelated metadata."""
    if actual == desired:
        return True
    if isinstance(desired, dict):
        return (actual is None and not desired) or isinstance(actual, dict) and all(
            _matches_declared_fields(actual.get(key), value) for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (actual is None and not desired) or isinstance(actual, list) and len(actual) == len(desired) and all(
            _matches_declared_fields(value, expected) for value, expected in zip(actual, desired, strict=True)
        )
    return False


def _owned_by_release(document: dict[str, Any], release: ReleaseRef) -> bool:
    """Match Helm ownership before bootstrapping or removing native file-origin resources."""
    metadata = document["metadata"]
    annotations = metadata.get("annotations", {})
    return (
        metadata.get("labels", {}).get("app.kubernetes.io/managed-by") == "Helm"
        and annotations.get("meta.helm.sh/release-name") == release.name
        and annotations.get("meta.helm.sh/release-namespace") == release.namespace
    )


def _value_at(values: dict[str, Any], path: str) -> Any:
    """Read a field whose shape is owned by the selected native chart."""
    value: Any = values
    for key in path.split("."):
        value = value[key]
    return value


def _merge_values(
    defaults: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
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
