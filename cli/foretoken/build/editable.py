# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build and select per-checkout source applications for deployment instances."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loguru import logger

from foretoken.applications.files import (
    SERVICE_COMPONENTS,
    SOURCE_REVISION,
    ApplicationFiles,
    ApplicationPublication,
)
from foretoken.arguments import InstallCommand
from foretoken.build.cluster.builder import ClusterBuilder, registry_credentials
from foretoken.build.cluster.compiler import APPLICATION_PARTS, CompilerWorkspace
from foretoken.build.cluster.images import image_tools_image, node_image_store
from foretoken.build.cluster.storage import ensure_build_volumes, local_build_nodes
from foretoken.build.inputs import (
    BuildInputs,
    build_sources,
    engine_source_roots,
    mooncake_build_version,
    snapshot_versions,
    source_checkout,
)
from foretoken.build.records import (
    BUILD_TRANSPORT_ARGUMENTS,
    SOURCE_BUILD_KEY,
    restore_build,
    retain_workspace,
)
from foretoken.build.source import build_arguments, prepare_source_images
from foretoken.build.state import (
    SOURCE_INSTALL_ANNOTATION,
    checkout_operation,
    checkout_state_directory,
    cluster_state_directory,
    context_identity,
    local_candidates,
    reconcile_source_resources,
    runtime_settings,
    write_source_state,
)
from foretoken.deployment.manifest import (
    ForetokenDeployment,
    service_requires_execution,
)
from foretoken.downloads.sources import select_build_sources
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, timeout_seconds
from foretoken.platform.config import default_platform_config
from foretoken.platform.helm import Helm
from foretoken.processes.execution import cleanup_commands

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
        """Resolve the platform's build recipe, borrowing only matching local installation history."""
        self.kubectl = kubectl
        self.cluster = cluster
        self.configuration = platform
        self.root = root
        self.directory = checkout_state_directory(cluster, root, timeout, context=context_identity(kubectl))
        self.engine_sources = engine_sources
        self.selected: dict[tuple[str, str, str], str] = {}
        installed = cluster / "install.json"
        self.platform = json.loads(installed.read_text()) if installed.is_file() else None
        if self.platform is not None and (
            self.platform["platform_uid"] != platform["metadata"]["uid"]
            or self.platform["runtime"] != runtime_settings(platform)
        ):
            self.platform = None
        # The cluster recipe supports new workstations; legacy local settings
        # are usable only after their platform identity and runtime matched.
        recipe = platform["data"].get(SOURCE_BUILD_KEY)
        if recipe is not None:
            self.recipe = json.loads(recipe)
        elif self.platform is not None:
            build = self.platform["build"]
            self.recipe = {
                "registry": build["registry"], "ociRegistry": self.platform["command"]["oci_registry"],
                "baseImage": self.platform["base_image"], "runtimeBackend": build["runtime_backend"],
                "buildMetaXRuntime": build["build_metax_runtime"], "configuration": build["configuration"],
            }
        else:
            raise DeploymentError("The installed platform has no source-build settings; its administrator must update the platform")
        self.origin = ApplicationFiles(kubectl, platform["metadata"]["namespace"])

    @classmethod
    def discover(
        cls, kubectl: Kubectl, timeout: str, editable: str | None = None,
        engine_sources: tuple[str, ...] = (),
    ) -> EditableDeployment | None:
        """Resolve explicit source development from the installed platform, independent of workstation history."""
        if editable is None and not engine_sources:
            return None
        platform = Helm(default_platform_config(), kubectl).platform_configuration()
        if platform["metadata"].get("annotations", {}).get("inference.foretoken.io/retiring") == "true":
            raise DeploymentError("The Foretoken platform is being uninstalled")
        if platform["metadata"].get("annotations", {}).get(SOURCE_INSTALL_ANNOTATION) != "source":
            raise DeploymentError("source deployment requires a source-installed platform")
        return cls(kubectl, cluster_state_directory(kubectl), platform, source_checkout(editable), engine_sources, timeout)

    @contextmanager
    def operation(self, timeout: str) -> Iterator[None]:
        """Own this workspace's builds and retain its environment through service submission."""
        with checkout_operation(self.directory, self.kubectl, timeout):
            self.origin.prepare(timeout)
            path = self.directory / "install.json"
            self.state = json.loads(path.read_text()) if path.is_file() else None
            if self.state is None:
                self.state = self._adopt_build(timeout)
            self.engines = engine_source_roots(self.engine_sources) if self.engine_sources else (self.state or {}).get("engines", {})
            self.build_arguments = None
            self.environment_changed = True
            self.mooncake_version = None
            previous_images = {}
            if self.state is not None:
                build = self.state["build"]
                platform_changed = (
                    self.state["platform_uid"] != self.configuration["metadata"]["uid"]
                    or self.state.get("base_image") != self.recipe["baseImage"]
                    or self.state["command"]["oci_registry"] != self.recipe["ociRegistry"]
                    or any(build[key] != self.recipe[field] for key, field in (
                        ("registry", "registry"), ("configuration", "configuration"),
                        ("runtime_backend", "runtimeBackend"), ("build_metax_runtime", "buildMetaXRuntime"),
                    ))
                )
                self.build_arguments = None if platform_changed else {**build["arguments"], **build_arguments(dict(os.environ))}
                if self.build_arguments is not None and self.recipe["buildMetaXRuntime"] and "MOONCAKE_VERSION" in os.environ:
                    self.build_arguments["MOONCAKE_VERSION"] = os.environ["MOONCAKE_VERSION"]
                self.mooncake_version = mooncake_build_version(
                    self.root, self.build_arguments or {},
                    runtime_backend=self.recipe["runtimeBackend"], build_metax_runtime=self.recipe["buildMetaXRuntime"],
                )
                manifest = self.directory / self.state["inputs"] / "engine/manifest.json"
                old_engines = set(json.loads(manifest.read_text())) if manifest.is_file() else set()
                self.environment_changed = platform_changed or old_engines != self.engines.keys() or (
                    {key: value for key, value in (self.build_arguments or {}).items() if key not in BUILD_TRANSPORT_ARGUMENTS}
                    != {key: value for key, value in build["arguments"].items() if key not in BUILD_TRANSPORT_ARGUMENTS}
                )
                if self.build_arguments is not None:
                    build["arguments"].update({
                        key: value for key, value in self.build_arguments.items() if key in BUILD_TRANSPORT_ARGUMENTS
                    })
                self.state["engines"] = self.engines
                self.state["command"]["editable"] = str(self.root)
                self.state["command"]["engine_sources"] = [f"{name}={root}" for name, root in self.engines.items()]
                self.state["runtime"].update({
                    key: value for key, value in runtime_settings(self.configuration).items()
                    if key not in _APPLICATION_IMAGE_KEYS.values()
                })
                write_source_state(path, self.state)
                previous_images = dict(build["images"])
            logger.info("Source checkout: {}", self.root)
            if (self.directory / "build.json").is_file():
                reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)
            self._retain_workspace()
            try:
                yield
            finally:
                with cleanup_commands(sys.exception(), timeout=timeout_seconds(timeout)):
                    self._retain_workspace()
            if self.state is not None and self.state["build"]["images"] != previous_images:
                reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)

    def prepare(self, deployment: ForetokenDeployment, timeout: str) -> None:
        """Publish required applications before the instance selects its controller and service defaults."""
        with local_candidates(self.directory):
            self._prepare(timeout)
            components = {"control-plane"}
            components.update(
                component for obj, component in _executing_services(deployment.objects)
                if component != "model-server" or obj["spec"].get("backend") == "vllm"
            )
            self._publish_applications(components, timeout)

    def apply(
        self, deployment: ForetokenDeployment, controller: dict[str, Any],
    ) -> ForetokenDeployment:
        """Select published applications against the instance controller's resolved defaults."""
        manager = next(
            container for container in controller["spec"]["template"]["spec"]["containers"]
            if container["name"] == "manager"
        )
        arguments = dict(
            argument.removeprefix("--").split("=", 1)
            for argument in manager["args"] if "=" in argument
        )
        # The selected controller keeps its defaults even when another workstation
        # updates the platform during this source operation.
        controller_applications = {
            "frontend": {
                "applicationURL": arguments.get("frontend-application-url", ""),
                "image": arguments["frontend-image"],
            },
            "model-server": {
                "applicationURL": arguments.get("model-server-application-url", ""),
                "image": arguments["inference-engine-image"],
            },
        }
        objects = copy.deepcopy(deployment.objects)
        for obj, component in _executing_services(objects):
            metadata = obj["metadata"]
            annotations = metadata.setdefault("annotations", {})
            annotations.pop(SOURCE_REVISION, None)
            if component == "model-server" and obj["spec"].get("backend") != "vllm":
                # Alternate backends keep their platform image and application.
                continue
            namespace = metadata.get("namespace") or deployment.namespace or "default"
            current = self.kubectl.get_if_exists(obj["kind"], metadata["name"], namespace)
            active = (current or {}).get("metadata", {}).get("annotations", {}).get(SOURCE_REVISION, "")
            bundle = self.state["bundles"].get(
                component, self.state["build"].get("applications", {}).get(component)
            )
            revision = bundle["revision"] if bundle is not None else ""
            # Entering source development must not replace an unchanged default
            # cohort. Existing source selections keep their explicit identity.
            if (
                revision and not active
                and self.origin.reference(component, revision) == controller_applications[component]["applicationURL"]
                and self.state["runtime"][_APPLICATION_IMAGE_KEYS[component]] == controller_applications[component]["image"]
            ):
                revision = ""
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
            if revision:
                metadata["annotations"][SOURCE_REVISION] = revision
        return deployment.with_objects(objects)

    @property
    def controller_source_revision(self) -> str:
        """Return the actual completed controller publication selected after prepare and apply."""
        bundle = self.state["bundles"].get("control-plane")
        if bundle is None:
            bundle = self.state["build"]["applications"]["control-plane"]
        return bundle["revision"]

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
        if any(kind == "ModelService" for kind, _, _ in self.selected):
            frontend = self.kubectl.get("frontendservice", deployment.frontend, deployment.namespace)
            metadata = frontend["metadata"]
            if not metadata.get("deletionTimestamp") and service_requires_execution(frontend):
                consumers.setdefault(
                    ("FrontendService", deployment.namespace, deployment.frontend),
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

    def _retain_workspace(self) -> None:
        """Publish only recovery references so other workstations can collect images without dropping this environment."""
        if self.state is None:
            return
        applications = {
            self.origin.reference(component, revision)
            for bundles in (self.state["build"]["applications"], self.state["bundles"])
            for component, bundle in bundles.items()
            for key in ("revision", "previous")
            if (revision := bundle.get(key))
        }
        retain_workspace(self.origin, self.directory.name, applications, self.state["build"]["images"].values())

    def _adopt_build(self, timeout: str) -> dict[str, Any] | None:
        """Start a workspace from completed platform inputs without borrowing mutable compiler state."""
        if selection := self.recipe.get("completedBuild"):
            retain_workspace(self.origin, self.directory.name, selection["applications"].values(), selection["images"].values())
            snapshot, record = restore_build(self.origin, selection, self.directory, timeout)
            build = record["build"]
            environment = dict(os.environ)
            if self.recipe["ociRegistry"]:
                environment["FORETOKEN_OCI_REGISTRY"] = self.recipe["ociRegistry"]
            sources = select_build_sources(environment)
            transport = build_arguments({**environment, **sources.environment})
            build["arguments"] = {
                **record["arguments"],
                **{key: value for key, value in transport.items() if key in BUILD_TRANSPORT_ARGUMENTS},
            }
            build["registry_mirrors"] = sources.registry_mirrors
        elif self.platform is not None and (self.cluster / self.platform["inputs"]).is_dir():
            snapshot = self.directory / ("inputs-" + uuid.uuid4().hex)
            shutil.copytree(self.cluster / self.platform["inputs"], snapshot)
            build = copy.deepcopy(self.platform["build"])
        else:
            return None
        build.update(binding=self.directory.name, caches=[], borrowed_images=True)
        build.pop("engine_cache_scope", None)
        # An archive-backed engine selection has its own stable directory; advancing
        # or collecting input snapshots must not remove the next build's source.
        engines = {}
        manifest = snapshot / "engine/manifest.json"
        if manifest.is_file():
            destination = self.directory / "engine-inputs"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(snapshot / "engine", destination)
            engines = engine_source_roots(tuple(f"{name}={destination / name}" for name in json.loads(manifest.read_text())))
        build["engine_caches"] = {
            "" if build["registry"] else self.origin.node: self.directory.name + "-" + uuid.uuid4().hex,
        } if engines else {}
        command = InstallCommand(
            values=(), editable=str(self.root), registry=self.recipe["registry"] or None,
            oci_registry=self.recipe["ociRegistry"], prometheus=None, frontend_mode=None,
            gateway_name="", gateway_namespace="", gateway_section_name="", timeout=timeout,
            engine_sources=tuple(f"{name}={path}" for name, path in engines.items()),
        )
        runtime = runtime_settings(self.configuration)
        runtime.update({key: build["images"][component] for component, key in _APPLICATION_IMAGE_KEYS.items()})
        return {
            "root": str(self.root), "context": context_identity(self.kubectl),
            "engines": engines, "command": asdict(command), "inputs": snapshot.name,
            "build": build, "bundles": copy.deepcopy(build["applications"]),
            "platform_uid": self.configuration["metadata"]["uid"],
            "base_image": self.recipe["baseImage"], "runtime": runtime,
        }

    def _prepare(self, timeout: str) -> None:
        """Compare this checkout's inputs and prepare its runtime application or environment update."""
        if self.environment_changed or not self.state["build"].get("compilers"):
            logger.info("Source plan: prepare this checkout's runtime environments")
            self._rebuild(timeout)
            return
        engines = self.state.get("engines", {})
        old = self.directory / self.state["inputs"]
        current = BuildInputs.select(self.root, engines, mooncake_version=self.mooncake_version)
        changed = current.changed(old)
        if not changed:
            logger.info("Source plan: reuse runtime artifacts (no source inputs changed)")
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
                name.endswith((".rs", ".lua")) or Path(name).name in {"Cargo.toml", "Cargo.lock"}
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
            logger.info("Source plan: prepare this checkout's runtime environments")
            self._rebuild(timeout)
            return
        if components:
            logger.info("Source plan: publish {} applications", ", ".join(sorted(components)))
        else:
            logger.info("Source plan: update source snapshot only")
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
            required = set(APPLICATION_PARTS[component])
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
        """Build this checkout through the existing publisher, including its first use on a workstation."""
        if self.state is not None and not (self.directory / "build.json").is_file():
            reconcile_source_resources(self.kubectl, timeout, checkout=self.directory)
        command = InstallCommand(
            values=(), editable=str(self.root), registry=self.recipe["registry"] or None,
            oci_registry=self.recipe["ociRegistry"], prometheus=None, frontend_mode=None,
            gateway_name="", gateway_namespace="", gateway_section_name="", timeout=timeout,
            engine_sources=tuple(f"{name}={root}" for name, root in self.engines.items()),
        )
        namespace = self.configuration["metadata"]["namespace"]
        origin = ApplicationFiles(self.kubectl, namespace)
        origin.prepare(timeout)
        installed = json.loads(self.configuration["data"]["deploymentValues"])
        values = [{
            "development": {"build": self.recipe["configuration"]},
            "imagePullSecrets": installed["imagePullSecrets"],
        }]
        with prepare_source_images(
            command, namespace, values, self.recipe["baseImage"],
            origin=origin, checkout=self.directory,
            components=("control-plane", "frontend", "model-server"),
            saved_arguments=self.build_arguments,
            runtime_backend=self.recipe["runtimeBackend"],
            build_metax_runtime=self.recipe["buildMetaXRuntime"],
        ) as prepared:
            destination = self.directory / prepared.inputs.name
            prepared.inputs.rename(destination)
            runtime = runtime_settings(self.configuration)
            runtime.update(control_plane_image=prepared.control_plane, image=prepared.frontend, model_image=prepared.model_server)
            self.state = {
                "root": str(self.root), "context": context_identity(self.kubectl),
                "engines": self.engines, "command": asdict(command),
                "inputs": destination.name, "build": prepared.build_state,
                "bundles": copy.deepcopy(prepared.build_state["applications"]),
                "platform_uid": self.configuration["metadata"]["uid"],
                "base_image": self.recipe["baseImage"], "runtime": runtime,
            }
            write_source_state(self.directory / "install.json", self.state)
            (self.directory / "build.json").unlink(missing_ok=True)
        self.build_arguments = dict(self.state["build"]["arguments"])
        self.mooncake_version = mooncake_build_version(
            self.root, self.build_arguments,
            runtime_backend=self.recipe["runtimeBackend"], build_metax_runtime=self.recipe["buildMetaXRuntime"],
        )
        self.environment_changed = False

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
        helm = Helm(default_platform_config(self.state["command"]["oci_registry"]), self.kubectl)
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
        build_engine = (
            "model-server" in missing
            and bool(self.state.get("engines"))
            and "engine" in self.state["bundles"]["model-server"].get("parts", APPLICATION_PARTS["model-server"])
        )
        compilers = {component: build["compilers"][component] for component in missing}
        if build_engine:
            compilers["engine"] = build["compilers"]["engine"]
        if not build["registry"]:
            with node_image_store(
                self.kubectl, namespace, origin.node, build["containerd_socket"],
                image_tools_image(build["arguments"], build.get("registry_mirrors", {})),
                timeout, binding=build["binding"], pull_secrets=origin.pull_secrets,
            ) as store:
                available = all(
                    (digest := build["environments"]["compiler-" + component]["digests"].get(origin.node))
                    and store.matches(image, digest)
                    for component, image in compilers.items()
                )
            if not available:
                logger.warning("Compiler image is unavailable on this node; restoring the development environment")
                self._rebuild(timeout)
                return
        logger.info("Preparing application files: {}", ", ".join(sorted(missing)))
        with self._application_workspace(
            origin, snapshot, compilers, helm, timeout,
        ) as (builder, references), ExitStack() as cache_resources:
            cache_images = {
                component: image for component in missing
                if (image := build["images"].get("compiler-cache-" + component))
                and set(self.state["bundles"][component].get("parts", APPLICATION_PARTS[component])).intersection({"go", "rust"})
                and not builder.has_native_state(component)
            }
            cache_seeds = {}
            cache_builder = None
            if cache_images:
                tools = image_tools_image(build["arguments"], build.get("registry_mirrors", {}))
                cache_builder = cache_resources.enter_context(ClusterBuilder(
                    self.kubectl, namespace, builder.claim, builder.mount, build["configuration"]["image"],
                    build["binding"], timeout, tools_image=tools, execution_claim=builder.execution_claim,
                    node=origin.node, pull_secrets=origin.pull_secrets,
                    registry_mirrors=build.get("registry_mirrors", {}), cancellation=builder.cancellation,
                    credentials=registry_credentials(
                        [tools, build["configuration"]["image"], *cache_images.values()],
                        registries=[build["arguments"].get("UV_IMAGE_REGISTRY", "")],
                    ),
                ))
                for component, image in cache_images.items():
                    cache = builder.prepare_cache_seed(cache_builder, component, build["arguments"], reference=image)
                    if cache is not None:
                        cache_seeds[component] = cache.directory

            def build_application(component: str) -> None:
                """Build a component's files and assemble its engine overlay before publication."""
                bundle = self.state["bundles"][component]
                revision = bundle["revision"]
                staging = builder.root + "/output/" + revision
                payload = staging + "/payload"
                parts = bundle.get("parts", APPLICATION_PARTS[component])
                arguments = dict(build["arguments"])
                if component == "model-server":
                    arguments["INFERENCE_ENGINE_IMAGE"] = self.state["runtime"]["model_image"]
                builder.compile_application(component, arguments, list(parts), cache_seed=cache_seeds.get(component))
                if component == "model-server" and build_engine:
                    builder.compile_engine(
                        compilers["engine"], build["arguments"],
                        cache_id=build["engine_caches"]["" if build["registry"] else origin.node],
                        build_native=build["engine_native"],
                    )
                builder.export_application(component, payload, engine=component == "model-server" and build_engine)

            # Each component owns its export directory. Join builds before validating
            # their shared source snapshot or allowing any publisher to consume them.
            with ThreadPoolExecutor(max_workers=len(missing)) as builds, builder.cancellation:
                pending_builds = [
                    builds.submit(builder.cancellation.run_stage, build_application, component)
                    for component in sorted(missing)
                ]
                for future in pending_builds:
                    future.result()
            if cache_builder is not None:
                cache_builder.run(["rm", "-rf", "--", cache_builder.execution_root + "/compiler-caches"])
            BuildInputs.select(
                self.root, self.state.get("engines", {}), mooncake_version=self.mooncake_version,
            ).validate(snapshot)
            # Join every publisher before committing selections or releasing their exports.
            published_at = time.monotonic()
            previous_revisions = {}
            publications = []
            for component in sorted(missing):
                bundle = self.state["bundles"][component]
                revision = bundle["revision"]
                previous = bundle.get("previous", "")
                previous_revisions[component] = previous
                publications.append(ApplicationPublication(
                    component=component,
                    source=builder.root + "/output/" + revision + "/payload",
                    revision=revision,
                    previous=previous,
                    sources=build_sources(snapshot, component),
                    image=self.state["runtime"][_APPLICATION_IMAGE_KEYS[component]],
                    parts=bundle.get("parts"),
                ))
            published = origin.publish_batch(
                builder, publications, references, timeout=timeout,
            )
            for component, published_revision in published.revisions.items():
                default_revision = (
                    build.get("applications", {}).get(component, {}).get("revision")
                )
                # Store only differences from this checkout's completed build.
                if published_revision == default_revision:
                    self.state["bundles"].pop(component)
                else:
                    self.state["bundles"][component] = {"revision": published_revision}
                publication = (
                    "reused"
                    if published_revision == previous_revisions[component]
                    else "published"
                )
                logger.info("Application {}: {}", publication, component)
            write_source_state(self.directory / "install.json", self.state)
            logger.info("Application publication completed in {:.1f}s", time.monotonic() - published_at)
            builder.run(["rm", "-rf", "--", builder.root + "/output"], container=builder.artifact_container)

    @contextmanager
    def _application_workspace(
        self,
        origin: ApplicationFiles,
        snapshot: Path,
        compilers: dict[str, str],
        helm: Helm,
        timeout: str,
    ) -> Iterator[tuple[CompilerWorkspace, set[str] | None]]:
        """Hold one origin compiler and its retention snapshot through incremental application publication."""
        namespace = origin.namespace
        build = self.state["build"]
        local_caches = local_build_nodes(
            self.kubectl, build["registry"], build["containerd_socket"]
        )
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
        if "engine" in compilers and not build["registry"] and origin.node not in build["engine_caches"]:
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
        tools_image = image_tools_image(build["arguments"], build.get("registry_mirrors", {}))
        with CompilerWorkspace(
            self.kubectl, namespace, claim, build["binding"], origin.node,
            {name: image for name, image in compilers.items() if name != "engine"}, build["configuration"], timeout,
            tools_image=tools_image,
            execution_claim=execution["name"] if execution is not None else "",
            owner={"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": origin.claim, "uid": origin.claim_uid},
            credentials=registry_credentials([tools_image, *compilers.values()]),
            pull_secrets=origin.pull_secrets,
        ) as builder:
            builder.sync(inputs, build["versions"])
            yield builder, references

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
                or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}
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
            and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
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


def _executing_services(objects: Iterable[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], str]]:
    """Select first-party services requesting execution for source preparation and activation."""
    for obj in objects:
        component = SERVICE_COMPONENTS.get(obj.get("kind"))
        if (
            component is not None
            and str(obj.get("apiVersion") or "").startswith("inference.foretoken.io/")
            and service_requires_execution(obj)
        ):
            yield obj, component
