# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare source-installed runtime updates without rebuilding their environment images."""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from foretoken.application_files import (
    SERVICE_COMPONENTS,
    SOURCE_REVISION,
    ApplicationFiles,
)
from foretoken.arguments import InstallCommand
from foretoken.build_storage import ensure_build_volumes, local_build_nodes
from foretoken.cluster_build import (
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
)
from foretoken.engine_environment import export_engine_source
from foretoken.kubernetes import Kubectl
from foretoken.manifest import (
    DeploymentError,
    ForetokenDeployment,
    ResourceRef,
    parse_deployment,
    service_requires_execution,
)
from foretoken.node_images import image_tools_image
from foretoken.platform.config import default_platform_config
from foretoken.platform.helm import Helm
from foretoken.source import prepare_source_images
from foretoken.source_inputs import (
    BuildInputs,
    build_sources,
    engine_source_roots,
    pinned_rust_revision,
    snapshot_versions,
    source_checkout,
)
from foretoken.source_state import (
    SOURCE_INSTALL_ANNOTATION,
    checkout_operation,
    checkout_state_directory,
    cluster_state_directory,
    has_server_binding,
    local_candidates,
    reconcile_source_resources,
    record_source_deployment,
    runtime_settings,
    write_source_state,
)

_APPLICATION_PARTS = {
    "control-plane": ("go",),
    "frontend": ("rust",),
    "model-server": ("rust", "python", "engine"),
}
_APPLICATION_IMAGE_KEYS = {
    "control-plane": "control_plane_image",
    "frontend": "image",
    "model-server": "model_image",
}


class EditableDeployment:
    """Own local source comparison, candidate preparation and target-service activation."""

    def __init__(
        self, kubectl: Kubectl, cluster: Path, platform: dict[str, Any],
        root: Path, engine_sources: tuple[str, ...], timeout: str,
    ) -> None:
        self.kubectl = kubectl
        self.cluster = cluster
        self.platform = platform
        self.root = root
        self.directory = checkout_state_directory(cluster, root, timeout)
        self.engine_sources = engine_sources
        self.selected: dict[tuple[str, str, str], str] = {}

    @classmethod
    def discover(
        cls, kubectl: Kubectl, timeout: str, editable: str | None = None,
        engine_sources: tuple[str, ...] = (),
    ) -> EditableDeployment | None:
        """Enable source updates only for a platform installed explicitly from source."""
        if not has_server_binding(kubectl):
            if editable is not None or engine_sources:
                raise DeploymentError("source deployment requires `foretoken install -e PATH` first")
            return None
        platform = Helm(default_platform_config()).platform_configuration(kubectl)
        if platform["metadata"].get("annotations", {}).get(SOURCE_INSTALL_ANNOTATION) != "source":
            if editable is not None or engine_sources:
                raise DeploymentError("source deployment requires a source-installed platform")
            return None
        directory = cluster_state_directory(kubectl)
        path = directory / "install.json"
        if not path.is_file():
            raise DeploymentError(
                "source checkout is not associated with this workstation; run `foretoken install -e .` from that checkout's root"
            )
        state = json.loads(path.read_text())
        if (
            state.get("platform_uid") != platform["metadata"]["uid"]
            or state["runtime"] != runtime_settings(platform)
        ):
            raise DeploymentError(
                "source installation changed; run `foretoken install -e .` from the intended checkout's root"
            )
        return cls(kubectl, directory, state, source_checkout(editable), engine_sources, timeout)

    @contextmanager
    def operation(self, deployment: ForetokenDeployment, timeout: str) -> Iterator[None]:
        """Register intended services and own this checkout's builds through service submission."""
        with checkout_operation(self.directory, self.kubectl, timeout):
            record_source_deployment(self.directory, deployment)
            path = self.directory / "install.json"
            saved = json.loads(path.read_text()) if path.is_file() else None
            if saved is None:
                self.state = copy.deepcopy(self.platform)
                snapshot = self.directory / ("inputs-" + uuid.uuid4().hex)
                shutil.copytree(self.cluster / self.platform["inputs"], snapshot)
                self.state.update(
                    root=str(self.root), inputs=snapshot.name,
                    platform_inputs=self.platform["inputs"],
                    bundles=copy.deepcopy(self.state["bundles"]),
                )
                self.state["build"]["binding"] = self.directory.name
                self.state["build"]["caches"] = []
                self.state["build"]["borrowed_images"] = True
                if str(self.root) != self.platform["root"]:
                    self.state["engines"] = {}
            else:
                self.state = saved
            platform_changed = (
                self.state["platform_uid"] != self.platform["platform_uid"]
                or self.state.get("platform_inputs") != self.platform["inputs"]
            )
            self.build_arguments = dict(self.state["build"]["arguments"])
            if platform_changed:
                # Refresh platform intent, retaining this checkout's input versions
                # and effective outputs until the environment build succeeds.
                self.state["command"] = copy.deepcopy(self.platform["command"])
                self.state["base_image"] = self.platform.get("base_image")
                self.build_arguments = dict(self.platform["build"]["arguments"])
                for key in ("configuration", "registry_mirrors", "runtime_backend", "build_metax_runtime"):
                    self.state["build"][key] = copy.deepcopy(self.platform["build"][key])
                self.state["build"]["environments"] = copy.deepcopy(
                    self.platform["build"].get("environments", {})
                )
                self.state["runtime"].update({
                    key: copy.deepcopy(value)
                    for key, value in self.platform["runtime"].items()
                    if key not in {"control_plane_image", "image", "model_image"}
                })
                # A checkout's first namespace controller uses the installed environment.
                self.state["runtime"].setdefault(
                    "control_plane_image", self.platform["runtime"]["control_plane_image"]
                )
            self.state["command"]["editable"] = str(self.root)
            engines = (
                engine_source_roots(self.engine_sources) if self.engine_sources
                else self.state.get("engines", {})
            )
            snapshot = self.directory / self.state["inputs"]
            manifest = snapshot / "engine/manifest.json"
            old_engines = set(json.loads(manifest.read_text())) if manifest.is_file() else set()
            if self.state["build"].get("build_metax_runtime") and "MOONCAKE_VERSION" in os.environ:
                self.build_arguments["MOONCAKE_VERSION"] = os.environ["MOONCAKE_VERSION"]
            self.mooncake_version = (
                self.build_arguments.get("MOONCAKE_VERSION", "")
                if self.state["build"].get("build_metax_runtime") else None
            )
            self.environment_changed = (
                platform_changed or old_engines != engines.keys()
                or self.build_arguments != self.state["build"]["arguments"]
            )
            self.state["engines"] = engines
            self.state["command"]["engine_sources"] = [
                f"{name}={path}" for name, path in engines.items()
            ]
            write_source_state(path, self.state)
            print(f"Source checkout: {self.root}", flush=True)
            if (self.directory / "build.json").is_file():
                reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)
            previous_images = dict(self.state["build"]["images"])
            yield
            if self.state["build"]["images"] != previous_images:
                reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)

    def prepare(self, timeout: str) -> None:
        """Prepare changed runtime artifacts and retire candidates not retained by committed state."""
        with local_candidates(self.directory):
            self._prepare(timeout)

    def _prepare(self, timeout: str) -> None:
        """Compare this checkout's inputs and prepare its runtime application or environment update."""
        if self.environment_changed or not self.state.get("build") or self.state["build"]["arguments"].get(
            "VLLM_REVISION"
        ) != pinned_rust_revision(self.root):
            print(
                "Source plan: prepare this checkout's runtime environments",
                flush=True,
            )
            self._rebuild(timeout)
            return
        engines = self.state.get("engines", {})
        old = self.directory / self.state["inputs"]
        current = BuildInputs.select(self.root, engines, old, mooncake_version=self.mooncake_version)
        changed = current.changed(old)
        if not changed:
            print(
                "Source plan: reuse runtime artifacts (no source inputs changed)",
                flush=True,
            )
            return
        components: dict[str, set[str]] = {}
        rebuild = False
        for name in changed:
            if name.startswith("engine/"):
                if Path(name).suffix in {".md", ".png", ".svg"}:
                    continue
                parts = Path(name).parts
                if len(parts) > 2 and (
                    parts[2]
                    in {
                        "requirements",
                        "pyproject.toml",
                        "setup.py",
                        "setup.cfg",
                        "Dockerfile",
                    }
                ):
                    rebuild = True
                else:
                    components.setdefault("model-server", set()).add("engine")
            elif name.startswith(("deploy/charts/", "deploy/grafana/")):
                # Templates and dashboards are applied by Helm, independently of application builds.
                continue
            elif name.startswith("control-plane/"):
                if name == "control-plane/Dockerfile":
                    rebuild = True
                else:
                    components.setdefault("control-plane", set()).add("go")
            elif name.startswith("data-plane/model-server/python/"):
                components.setdefault("model-server", set()).add("python")
            elif name.startswith("data-plane/") and (
                name.endswith(".rs") or Path(name).name in {"Cargo.toml", "Cargo.lock"}
            ):
                targets = (
                    {"frontend"}
                    if name.startswith("data-plane/frontend/")
                    else {"model-server"}
                    if name.startswith("data-plane/model-server/")
                    else {"frontend", "model-server"}
                )
                for component in targets:
                    components.setdefault(component, set()).add("rust")
            else:
                rebuild = True
        if rebuild:
            print(
                "Source plan: prepare this checkout's runtime environments",
                flush=True,
            )
            self._rebuild(timeout)
            return
        if components:
            print(
                "Source plan: publish "
                + ", ".join(sorted(components))
                + " applications",
                flush=True,
            )
        else:
            print("Source plan: update source snapshot only", flush=True)
        snapshot = self.directory / ("inputs-" + str(uuid.uuid4()))
        current.capture(snapshot, previous=old, unchanged=set(current.files).difference(changed))
        self.state["build"]["versions"] = snapshot_versions(
            snapshot, old, self.state["build"]["versions"], changed
        )
        bundles = dict(self.state["bundles"])
        for component, parts in components.items():
            prior = bundles.get(
                component,
                self.state["build"].get("applications", {}).get(component, {}),
            )
            bundle = {
                "revision": str(uuid.uuid4()),
                "previous": prior.get("revision", ""),
            }
            required = set(_APPLICATION_PARTS[component])
            if not engines:
                required.discard("engine")
            if parts < required and bundle["previous"]:
                origin = ApplicationFiles(
                    self.kubectl,
                    default_platform_config(self.state["command"]["oci_registry"]).namespace,
                )
                manifest = origin.read_manifest(origin.reference(component, bundle["previous"]))
                image = self.state["runtime"][_APPLICATION_IMAGE_KEYS[component]]
                # Only a completed publication represents the prior inputs. Pending
                # or deferred updates require a full build, even after another edit.
                if manifest is not None and manifest.get("image") == image:
                    bundle["parts"] = sorted(parts)
            bundles[component] = bundle
        self.state.update(inputs=snapshot.name, bundles=bundles)
        write_source_state(self.directory / "install.json", self.state)

    def _rebuild(self, timeout: str) -> None:
        """Build candidate environments and applications without changing the shared platform."""
        # Reclaim unused environments before allocating another. Interrupted
        # builds were already reconciled when entering the checkout operation.
        if not (self.directory / "build.json").is_file():
            reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)
        settings = dict(self.state["command"])
        settings.update(values=(), timeout=timeout, editable=str(self.root))
        command = InstallCommand(**settings)
        namespace = default_platform_config(command.oci_registry).namespace
        origin = ApplicationFiles(self.kubectl, namespace)
        origin.prepare(timeout)
        build = self.state["build"]
        runtime = self.state["runtime"]
        values = [{
            "development": {"build": build["configuration"]},
            "imagePullSecrets": [{"name": name} for name in runtime.get("pull_secrets", [])],
        }]
        with prepare_source_images(
            command, namespace, values, self.state.get("base_image"),
            origin=origin, checkout=self.directory,
            components=("control-plane", "frontend", "model-server"),
            saved_arguments=self.build_arguments,
            runtime_backend=build.get("runtime_backend", "nvidia"),
            build_metax_runtime=build.get("build_metax_runtime", False),
        ) as prepared:
            destination = self.directory / prepared.inputs.name
            prepared.inputs.rename(destination)
            runtime.update(
                control_plane_image=prepared.control_plane,
                image=prepared.frontend,
                model_image=prepared.model_server,
            )
            self.state.update(
                inputs=destination.name, build=prepared.build_state,
                bundles=prepared.build_state["applications"],
                platform_uid=self.platform["platform_uid"],
                platform_inputs=self.platform["inputs"],
            )
            write_source_state(self.directory / "install.json", self.state)
            (self.directory / "build.json").unlink(missing_ok=True)
        self.environment_changed = False

    def apply(
        self, deployment: ForetokenDeployment, timeout: str
    ) -> ForetokenDeployment:
        """Publish selected applications before submitting service and model-storage intent."""
        objects = copy.deepcopy(deployment.objects)
        pending: set[str] = {"control-plane"}
        services = []
        for obj in objects:
            component = SERVICE_COMPONENTS.get(obj.get("kind"))
            if component is None or not str(obj.get("apiVersion") or "").startswith(
                "inference.foretoken.io/"
            ):
                continue
            # Retain pending bundles until the service requests execution.
            if not service_requires_execution(obj):
                continue
            metadata = obj["metadata"]
            annotations = metadata.setdefault("annotations", {})
            annotations.pop(SOURCE_REVISION, None)
            bundle = self.state["bundles"].get(
                component, self.state["build"].get("applications", {}).get(component)
            )
            namespace = metadata.get("namespace") or deployment.namespace or "default"
            if component == "model-server" and obj["spec"].get("backend") != "vllm":
                # Alternate backends keep their own platform image/profile, rather
                # than receiving the standard vLLM application's environment.
                continue
            current = self.kubectl.get_if_exists(
                obj["kind"], metadata["name"], namespace
            )
            active = (
                (current or {})
                .get("metadata", {})
                .get("annotations", {})
                .get(SOURCE_REVISION, "")
            )
            if bundle is not None and active != bundle["revision"]:
                pending.add(component)
            services.append((obj, component, namespace, current, active))
        if pending:
            self._publish_applications(pending, timeout)

        # Publication can reuse an earlier version. Select workloads only after the
        # publisher has committed the actual application references.
        for obj, component, namespace, current, active in services:
            metadata = obj["metadata"]
            bundle = self.state["bundles"].get(
                component, self.state["build"].get("applications", {}).get(component)
            )
            revision = bundle["revision"] if bundle is not None else ""
            writers = (
                self._ready_containers(namespace, component, metadata["name"], revision)
                if current is not None
                else []
            )
            ready = bool(writers)
            if component == "frontend":
                ready = ready and len(writers) == obj["spec"].get("replicas", 1)
            # Platform image changes do not advance Service generation; old Ready status
            # must not make a deployment finish before the selected runtime is serving.
            if active != revision or not ready:
                self.selected[(obj["kind"], namespace, metadata["name"])] = revision
            if bundle is not None:
                metadata["annotations"][SOURCE_REVISION] = revision
        return parse_deployment(
            deployment.path, yaml.safe_dump_all(objects, sort_keys=False)
        )

    def deployment_values(self, installed: dict[str, Any]) -> dict[str, Any]:
        """Select this checkout's published controller without changing platform defaults."""
        values = copy.deepcopy(installed)
        reference = self.state["runtime"]["control_plane_image"]
        repository, separator, digest = reference.partition("@")
        if separator:
            values["image"].update(repository=repository, digest=digest, tag="")
        else:
            repository, tag = reference.rsplit(":", 1)
            values["image"].update(repository=repository, tag=tag, digest="")
        bundle = self.state["bundles"].get(
            "control-plane", self.state["build"]["applications"]["control-plane"]
        )
        origin = ApplicationFiles(
            self.kubectl,
            default_platform_config(self.state["command"]["oci_registry"]).namespace,
        )
        values["controller"]["applicationURL"] = origin.reference(
            "control-plane", bundle["revision"]
        )
        return values

    def _publish_applications(self, components: set[str], timeout: str) -> None:
        """Publish selected components once, independently of their model namespaces and caches."""
        namespace = default_platform_config(self.state["command"]["oci_registry"]).namespace
        origin = ApplicationFiles(self.kubectl, namespace)
        pending = set(components)
        missing = {
            component
            for component in pending
            if (bundle := self.state["bundles"].get(component)) is not None
            and origin.read_manifest(origin.reference(component, bundle["revision"]))
            is None
        }
        snapshot = self.directory / self.state["inputs"]
        helm = Helm(default_platform_config(self.state["command"]["oci_registry"]))
        # A completed application no longer depends on its partial-publication base.
        for component in pending - missing:
            if bundle := self.state["bundles"].get(component):
                self.state["bundles"][component] = {"revision": bundle["revision"]}
        if not missing:
            write_source_state(self.directory / "install.json", self.state)
            return

        origin.prepare(timeout)
        for component in missing:
            bundle = self.state["bundles"][component]
            if bundle.get("parts") and origin.read_manifest(
                origin.reference(component, bundle["previous"])
            ) is None:
                bundle.pop("parts")
        build = self.state["build"]
        mount = "/var/cache/foretoken"
        claim = find_build_cache(
            self.kubectl, namespace, build["binding"], origin.node, mount
        )
        local_caches = local_build_nodes(
            self.kubectl, build["registry"], build["containerd_socket"]
        )
        socket = next(
            (socket for node, socket, _ in local_caches if node == origin.node), ""
        )
        if claim is None:
            node_uid = self.kubectl.get("node", origin.node)["metadata"]["uid"][:8]
            claim = next(
                (claim for node, _, claim in local_caches if node == origin.node),
                "foretoken-application-compiler-" + node_uid,
            ) + "-" + build["binding"][:12]

        def record_cache(identity: dict[str, Any]) -> None:
            """Keep each volume UID recoverable if paired allocation or publication fails."""
            if identity not in build.setdefault("caches", []):
                build["caches"].append(identity)
                write_source_state(self.directory / "install.json", self.state)

        _, execution = ensure_build_volumes(
            self.kubectl,
            namespace,
            claim,
            build["configuration"],
            binding=build["binding"],
            timeout=timeout,
            record_identity=record_cache,
            recorded_caches=build.get("caches", []),
            owner={
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": origin.claim,
                "uid": origin.claim_uid,
            },
        )
        build_engine = (
            "model-server" in missing
            and bool(self.state.get("engines"))
            and "engine" in self.state["bundles"]["model-server"].get(
                "parts", _APPLICATION_PARTS["model-server"]
            )
        )
        if build_engine and not build["registry"] and origin.node not in build["engine_caches"]:
            # A newly prepared origin node needs a cold cache, not another node's native outputs.
            build["engine_caches"][origin.node] = (
                build["binding"] + "-" + uuid.uuid4().hex
            )
            write_source_state(self.directory / "install.json", self.state)
        inputs = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        references = origin.references(helm.application_history())
        if references is not None:
            references.update(
                bundle["revision"] for bundle in self.state["bundles"].values()
            )
        print("Preparing application files: " + ", ".join(sorted(missing)), flush=True)
        with ClusterBuilder(
            self.kubectl,
            namespace,
            claim,
            mount,
            build["configuration"]["image"],
            build["binding"],
            timeout,
            tools_image=image_tools_image(
                build["arguments"], build.get("registry_mirrors", {})
            ),
            registry_mirrors=build.get("registry_mirrors", {}),
            execution_claim=execution["name"] if execution is not None else "",
            node=origin.node,
            containerd_socket=socket if build_engine else "",
            credentials=registry_credentials(
                [
                    build["configuration"]["image"],
                    self.state["runtime"]["model_image"],
                    self.state.get("base_image") or "",
                    build["arguments"].get("UV_IMAGE", ""),
                    "docker.io",
                    "gcr.io",
                    "ghcr.io",
                    *(
                        endpoint
                        for endpoints in build.get("registry_mirrors", {}).values()
                        for endpoint in endpoints
                    ),
                    *(
                        value
                        for key, value in build["arguments"].items()
                        if key.endswith("REGISTRY")
                    ),
                ]
            ),
            pull_secrets=origin.pull_secrets,
        ) as builder:
            builder.sync(inputs, build["versions"])

            def build_application(component: str) -> None:
                """Build a component's files and assemble its engine overlay before publication."""
                bundle = self.state["bundles"][component]
                revision = bundle["revision"]
                staging = builder.root + "/output/" + revision
                payload = staging + "/payload"
                parts = bundle.get("parts", _APPLICATION_PARTS[component])
                arguments = dict(build["arguments"])
                if component == "model-server":
                    arguments["INFERENCE_ENGINE_IMAGE"] = self.state["runtime"]["model_image"]
                builder.export_application(component, payload, arguments, list(parts))
                if component == "model-server" and build_engine:
                    export_engine_source(
                        builder, payload, build["arguments"],
                        runtime_image=self.state["runtime"]["model_image"],
                        cache_id=build["engine_caches"]["" if build["registry"] else origin.node],
                        build_native=build["engine_native"],
                    )

            # Each component owns its export directory. Join builds before validating
            # their shared source snapshot or allowing any publisher to consume them.
            with ThreadPoolExecutor(max_workers=len(missing)) as builds, builder.cancellation:
                pending_builds = [
                    builds.submit(builder.cancellation.run_stage, build_application, component)
                    for component in sorted(missing)
                ]
                for future in pending_builds:
                    future.result()
            BuildInputs.select(
                self.root, self.state.get("engines", {}), snapshot,
                mooncake_version=self.mooncake_version,
            ).validate(snapshot)
            # Join every publisher before committing selections or releasing their exports.
            published_at = time.monotonic()
            previous_revisions = {}
            with ThreadPoolExecutor(max_workers=len(missing)) as publications, builder.cancellation:
                futures = {}
                for component in sorted(missing):
                    bundle = self.state["bundles"][component]
                    revision = bundle["revision"]
                    previous = bundle.get("previous", "")
                    previous_revisions[component] = previous
                    futures[component] = publications.submit(
                        builder.cancellation.run_stage, origin.publish,
                        builder,
                        builder.root + "/output/" + revision + "/payload",
                        component,
                        revision,
                        previous,
                        references,
                        timeout=timeout,
                        reuse_previous=True,
                        sources=build_sources(snapshot, component),
                        image=self.state["runtime"][_APPLICATION_IMAGE_KEYS[component]],
                        parts=bundle.get("parts"),
                    )
                published = {
                    component: future.result() for component, future in futures.items()
                }
            for component, published_revision in published.items():
                default_revision = (
                    build.get("applications", {}).get(component, {}).get("revision")
                )
                # Matching the installed default needs no source override; adding one
                # would change Pod annotations even though the application is unchanged.
                if published_revision == default_revision:
                    self.state["bundles"].pop(component)
                else:
                    self.state["bundles"][component] = {"revision": published_revision}
                publication = (
                    "reused"
                    if published_revision == previous_revisions[component]
                    else "published"
                )
                print(f"Application {publication}: {component}", flush=True)
            write_source_state(self.directory / "install.json", self.state)
            print(
                f"Application publication completed in {time.monotonic() - published_at:.1f}s",
                flush=True,
            )
            builder.run(["rm", "-rf", "--", builder.root + "/output"], container=builder.artifact_container)

    def _ready_containers(
        self, namespace: str, component: str, service: str, revision: str
    ) -> list[tuple[str, str, str, str]]:
        """Select ready containers and route identities from the committed source or image cohort."""
        containers = []
        selected = {}
        pool_sizes: dict[str, int] = {}
        group_sizes: dict[str, int] = {}
        ready_members: dict[str, dict[str, int]] = {}
        if component == "model-server":
            current = self.kubectl.get("modelservice", service, namespace)
            selected = {
                p["poolUID"]: p["revision"]
                for p in current.get("status", {}).get("servingPoolRevisions", [])
            }
        else:
            current = self.kubectl.get("frontendservice", service, namespace)
            application = current.get("status", {}).get("application")
            if application is not None:
                frontend_image = application["image"]
            else:
                deployment = self.kubectl.get_if_exists(
                    "deployment", service, namespace
                )
                if deployment is None:
                    return []
                frontend_image = deployment["spec"]["template"]["spec"]["containers"][
                    0
                ]["image"]
        for pod in self.kubectl.list_resources(("pods",), namespace):
            metadata = pod["metadata"]
            if (
                metadata.get("deletionTimestamp")
                or metadata.get("annotations", {}).get(SOURCE_REVISION, "") != revision
            ):
                continue
            if not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            ):
                continue
            labels = metadata.get("labels", {})
            expected_image = frontend_image if component == "frontend" else ""
            route_target = ""
            if component == "frontend":
                if labels.get("inference.foretoken.io/frontend-service") != service:
                    continue
            else:
                group_name = labels.get("inference.foretoken.io/model-group")
                if not group_name:
                    continue
                group = self.kubectl.get_if_exists("modelgroup", group_name, namespace)
                if group is None:
                    continue
                pool = self.kubectl.get_if_exists(
                    "modelpool", group["spec"]["modelPoolRef"]["name"], namespace
                )
                if (
                    pool is None
                    or group["spec"]["modelPoolRef"]["uid"] != pool["metadata"]["uid"]
                ):
                    continue
                runtime = group["spec"]["runtime"]
                if (
                    pool["spec"]["modelServiceRef"]["name"] != service
                    or selected.get(pool["metadata"]["uid"])
                    != group["spec"]["revision"]
                    or runtime.get("sourceRevision", "") != revision
                ):
                    continue
                expected_image = runtime["image"]
                pool_uid = pool["metadata"]["uid"]
                pool_sizes[pool_uid] = pool["spec"]["desiredGroups"]
                route_target = group["metadata"]["uid"]
                group_sizes[route_target] = group["spec"]["memberCount"]
            for container in pod["spec"]["containers"]:
                if (
                    container["name"] == component
                    and container["image"] == expected_image
                ):
                    directory = next(
                        (
                            e.get("value", "")
                            for e in container.get("env", [])
                            if e["name"] == "FORETOKEN_SOURCE_DIRECTORY"
                        ),
                        "",
                    )
                    containers.append(
                        (metadata["name"], component, directory, route_target)
                    )
                    if component == "model-server":
                        members = ready_members.setdefault(pool_uid, {})
                        members[route_target] = members.get(route_target, 0) + 1
        if component == "model-server":
            if set(ready_members) != set(selected):
                return []
            if any(
                len(groups) != pool_sizes[pool_uid]
                or any(
                    count != group_sizes[group_uid]
                    for group_uid, count in groups.items()
                )
                for pool_uid, groups in ready_members.items()
            ):
                return []
        return containers

    def _frontend_routes_ready(
        self,
        namespace: str,
        service: str,
        writers: list[tuple[str, str, str, str]],
        route_targets: set[str],
        deadline: float,
    ) -> bool:
        """Observe the existing routing publication, consumer acknowledgements and Service endpoints."""
        deployment = self.kubectl.get("deployment", service, namespace)
        config_name = next(
            volume["configMap"]["name"]
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == "serving"
        )
        config_map = self.kubectl.get_if_exists("configmap", config_name, namespace)
        if config_map is None:
            return False
        snapshot = json.loads(config_map["data"]["serving.json"])
        published = {
            route["route_target_id"]
            for section in ("groups", "pd_components", "epd_components")
            for route in (snapshot.get(section) or [])
        }
        if not route_targets <= published:
            return False
        pods = [
            pod
            for pod in self.kubectl.list_resources(("pods",), namespace)
            if not pod["metadata"].get("deletionTimestamp")
            and pod["metadata"]
            .get("labels", {})
            .get("inference.foretoken.io/frontend-service")
            == service
        ]
        if len(pods) < deployment["spec"]["replicas"] or {
            pod["metadata"]["name"] for pod in pods
        } != {name for name, _, _, _ in writers}:
            return False
        for pod in pods:
            container = next(
                c for c in pod["spec"]["containers"] if c["name"] == "frontend"
            )
            port = next(
                port["containerPort"]
                for port in container["ports"]
                if port["name"] == "http"
            )
            state = json.loads(
                self.kubectl.get_raw(
                    f"/api/v1/namespaces/{namespace}/pods/http:{pod['metadata']['name']}:{port}/proxy/statusz",
                    f"{max(1, int(deadline - time.monotonic()))}s",
                )
            )
            if (
                not state["serving_ready"]
                or state["active_generation"] != snapshot["version"]
            ):
                return False
        endpoints = {
            endpoint.get("targetRef", {}).get("uid")
            for value in self.kubectl.list_resources(("endpointslices",), namespace)
            if value["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            == service
            for endpoint in value.get("endpoints", [])
            if endpoint.get("conditions", {}).get("ready") is True
        }
        return bool(pods) and {pod["metadata"]["uid"] for pod in pods} <= endpoints

    def verify(
        self,
        deployment: ForetokenDeployment,
        deadline: float,
        *,
        observe: Callable[[], None] | None = None,
    ) -> None:
        """Observe code and routing activation within DeploymentLifecycle's readiness deadline."""
        routes: dict[str, set[str]] = {}
        consumers = dict(self.selected)
        # Unchanged frontend code still needs to consume a new backend cohort.
        namespaces = {
            namespace for kind, namespace, _ in self.selected if kind == "ModelService"
        }
        for namespace in namespaces:
            for frontend in self.kubectl.list_resources(
                ("frontendservices",), namespace
            ):
                metadata = frontend["metadata"]
                if (
                    not metadata.get("deletionTimestamp")
                    and frontend["spec"].get("replicas", 1) > 0
                ):
                    consumers.setdefault(
                        ("FrontendService", namespace, metadata["name"]),
                        metadata.get("annotations", {}).get(SOURCE_REVISION, ""),
                    )
        # Verify committed backend code first, then the routing consumers of that cohort.
        selected = sorted(
            consumers.items(), key=lambda item: item[0][0] == "FrontendService"
        )
        for (kind, namespace, service), revision in selected:
            component = SERVICE_COMPONENTS[kind]
            code_changed = (kind, namespace, service) in self.selected
            if component == "frontend" and code_changed:
                self.kubectl.rollout_status(
                    ResourceRef("Deployment", service, namespace),
                    f"{max(1, int(deadline - time.monotonic()))}s",
                )
            while True:
                if observe is not None:
                    observe()
                writers = self._ready_containers(
                    namespace, component, service, revision
                )
                if writers and (
                    component != "frontend"
                    or self._frontend_routes_ready(
                        namespace,
                        service,
                        writers,
                        routes.get(namespace, set()),
                        deadline,
                    )
                ):
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"timed out waiting for {namespace}/{service} to activate source {revision}"
                    )
                time.sleep(min(2, max(0.0, deadline - time.monotonic())))
            if component == "model-server":
                routes.setdefault(namespace, set()).update(
                    route for _, _, _, route in writers
                )
            if not code_changed:
                continue
            for pod, container, directory, _ in writers:
                expected = (
                    f"FORETOKEN_ACTIVE_SOURCE_DIRECTORY={directory}"
                    if directory
                    else ""
                )
                output = self.kubectl.run(
                    [
                        "exec",
                        "-n",
                        namespace,
                        pod,
                        "-c",
                        container,
                        "--",
                        "sh",
                        "-c",
                        'grep -z "^FORETOKEN_ACTIVE_SOURCE_DIRECTORY=" /proc/1/environ; status=$?; test "$status" -le 1',
                    ]
                ).stdout.strip("\x00\n")
                if output != expected:
                    raise DeploymentError(
                        f"{namespace}/{pod} did not activate source {revision}"
                    )
