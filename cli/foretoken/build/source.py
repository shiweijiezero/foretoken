# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compose source environments and publish their completed applications."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken.applications.files import ApplicationFiles, ApplicationPublication
from foretoken.arguments import InstallCommand
from foretoken.build.cluster.builder import (
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
)
from foretoken.build.engine import (
    build_engine_environment,
    build_metax_environment,
    engine_cache_scope,
    export_engine_source,
    prepare_mooncake_source,
    resolve_vllm_base_image,
)
from foretoken.build.environments import BuildEnvironments
from foretoken.build.inputs import (
    SNAPSHOT_METADATA,
    BuildInputs,
    build_sources,
    engine_source_roots,
    pinned_rust_revision,
    snapshot_versions,
    source_checkout,
)
from foretoken.build.cluster.images import image_tools_image
from foretoken.build.state import (
    capture_build_inputs,
    cluster_state_directory,
    context_identity,
    local_candidates,
    write_source_state,
)
from foretoken.build.cluster.storage import (
    build_configuration,
    ensure_build_volumes,
    local_build_nodes,
    prepare_build_storage,
)
from foretoken.command_execution import CommandExecution
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.network_sources import select_build_sources

_LOCAL_IMAGE_PREFIX = "docker.io/library/foretoken-dev"


@dataclass(frozen=True)
class SourceImages:
    """Built environments, application references and inputs for installation or service deployment."""

    source_root: Path
    image_mode: str
    control_plane: str
    frontend: str
    model_server: str
    inputs: Path
    build_state: dict[str, Any]
    applications: dict[str, str]


@dataclass(frozen=True)
class _PreparedSourceBuild:
    """Resolved build history and recorded compiler storage ready for workspace capture."""

    previous_state: dict[str, Any]
    previous_build: dict[str, Any]
    previous_inputs: Path | None
    compiler_configuration: dict[str, str]
    pending_path: Path
    pending_state: dict[str, Any]
    nodes: list[tuple[str, str, str]]
    execution_claims: dict[str, str]
    registry: str
    revision: str
    mooncake_version: str | None


@dataclass(frozen=True)
class _SourceWorkspaces:
    """Captured inputs and live compiler workspaces held by the caller's artifact stack."""

    snapshot: Path
    versions: dict[str, str]
    builders: list[ClusterBuilder]
    publisher: ClusterBuilder
    base_image: str | None


@dataclass(frozen=True)
class _BuiltApplications:
    """Completed component environments and exports awaiting publication and image delivery."""

    environments: BuildEnvironments
    component_environments: dict[str, dict[str, Any]]
    engine_caches: dict[str, str]
    engine_native: bool
    application_parts: dict[str, list[str] | None]
    previous_revisions: dict[str, str]


@contextmanager
def prepare_source_images(
    command: InstallCommand,
    namespace: str,
    values: Sequence[dict[str, Any]],
    inference_engine_image: str | None = None,
    *,
    origin: ApplicationFiles,
    build_metax_runtime: bool = False,
    runtime_backend: str = "nvidia",
    saved_arguments: dict[str, str] | None = None,
    before_delivery: Callable[[], None] | None = None,
    checkout: Path | None = None,
    components: tuple[str, ...] = ("control-plane", "frontend", "model-server"),
    cancellation: CommandExecution | None = None,
) -> Iterator[SourceImages]:
    """Prepare installation artifacts, awaiting dependencies before node image delivery.

    The platform supplies before_delivery when preparing dependencies concurrently.
    Captured inputs remain in pending state until the installation or checkout commits them.
    """
    cancellation = cancellation or CommandExecution.current() or CommandExecution(timeout_seconds(command.timeout))
    cancellation.check()
    root = source_checkout(command.editable)
    kubectl = origin.kubectl
    engines = engine_source_roots(command.engine_sources)

    # Record source and volume ownership before any compiler can start.
    prepared = _prepare_source_build(
        root, engines, command, kubectl, namespace, values, origin,
        inference_engine_image=inference_engine_image,
        build_metax_runtime=build_metax_runtime,
        saved_arguments=saved_arguments, checkout=checkout,
        components=components, cancellation=cancellation,
    )
    with capture_build_inputs(
        root, engines, prepared.previous_inputs, mooncake_version=prepared.mooncake_version,
    ) as snapshot, ExitStack() as build_contexts:
        workspaces = _prepare_workspaces(
            root, prepared, snapshot, build_contexts, kubectl, namespace, origin,
            inference_engine_image=inference_engine_image,
            build_metax_runtime=build_metax_runtime,
            timeout=command.timeout, cancellation=cancellation,
        )
        built = _build_applications(
            engines, prepared, workspaces, origin,
            components=components, runtime_backend=runtime_backend,
            build_metax_runtime=build_metax_runtime, refresh=checkout is None,
            cancellation=cancellation,
        )
        images = {component: built.component_environments[component]["image"] for component in components}
        digests: dict[str, dict[str, str]] = {builder.node: {} for builder in workspaces.builders}
        for component, record in built.component_environments.items():
            for node, digest in record["digests"].items():
                digests[node][component] = digest

        # Publishers consume completed exports and finish before selection or cleanup.
        revisions = _publish_applications(
            prepared, workspaces, built, images, origin,
            checkout=checkout, oci_registry=command.oci_registry, timeout=command.timeout,
        )
        applications = {
            component: origin.reference(component, revision)
            for component, revision in revisions.items()
        }
        if before_delivery is not None:
            before_delivery()
        environment_images = _deliver_environments(workspaces, built, images, digests)

        # Join and close compilers before validating the source and handing artifacts
        # back to the installation or checkout owner that will commit their selection.
        build_contexts.close()
        BuildInputs.select(
            root, engines, workspaces.snapshot, mooncake_version=prepared.mooncake_version,
        ).validate(workspaces.snapshot)
        settings = prepared.pending_state["build"]
        yield SourceImages(
            root,
            "registry" if prepared.registry else "import",
            images.get("control-plane", ""),
            images["frontend"],
            images["model-server"],
            workspaces.snapshot,
            {
                "binding": settings["binding"],
                "caches": settings["caches"],
                "configuration": prepared.compiler_configuration,
                "versions": workspaces.versions,
                "digests": digests,
                "images": {**images, **environment_images},
                "environments": built.environments.records,
                "arguments": settings["arguments"],
                "registry_mirrors": settings["registry_mirrors"],
                "registry": prepared.registry,
                "containerd_socket": prepared.nodes[0][1],
                "engine_caches": built.engine_caches,
                **({"engine_cache_scope": settings["engine_cache_scope"]} if "engine_cache_scope" in settings else {}),
                "engine_native": built.engine_native,
                "runtime_backend": runtime_backend,
                "build_metax_runtime": build_metax_runtime,
                "applications": {
                    component: {"revision": revision}
                    for component, revision in revisions.items()
                },
            },
            applications,
        )


def _prepare_source_build(
    root: Path,
    engines: dict[str, str],
    command: InstallCommand,
    kubectl: Kubectl,
    namespace: str,
    values: Sequence[dict[str, Any]],
    origin: ApplicationFiles,
    *,
    inference_engine_image: str | None,
    build_metax_runtime: bool,
    saved_arguments: dict[str, str] | None,
    checkout: Path | None,
    components: tuple[str, ...],
    cancellation: CommandExecution,
) -> _PreparedSourceBuild:
    """Resolve build inputs and persist this operation's compiler identities before allocation."""
    state_directory = checkout if checkout is not None else cluster_state_directory(kubectl)
    state_path = state_directory / "install.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    build = previous.get("build", {}) if previous.get("root") == str(root) else {}
    pending_path = state_directory / "build.json"
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    if not build and pending.get("root") == str(root):
        build = pending["build"]
    binding = (
        checkout.name if checkout is not None
        else build.get("binding") or previous.get("build", {}).get("binding") or uuid.uuid4().hex
    )
    cache_scope = None
    if engines:
        pending_build = (
            pending.get("build", {})
            if pending.get("root") == str(root) and pending.get("build", {}).get("binding") == binding
            else {}
        )
        saved_scope = pending_build.get("engine_cache_scope") or build.get("engine_cache_scope")
        cache_scope = engine_cache_scope(binding, engines, saved_scope)
    arguments, registry_mirrors, environment = _select_build_arguments(command, build, saved_arguments)
    old = state_directory / previous["inputs"] if previous.get("inputs") else None
    mooncake_version = arguments.get("MOONCAKE_VERSION", "") if build_metax_runtime else None
    if mooncake_version is not None:
        prepare_mooncake_source(root, old, mooncake_version, environment, cancellation)
    arguments["VLLM_REVISION"] = pinned_rust_revision(root)
    configuration = build_configuration(
        root, values,
        arguments.get("BASE_IMAGE_REGISTRY", registry_mirrors.get("docker.io", [""])[0]),
    )
    registry = (command.registry or "").rstrip("/")
    prefix = registry if registry else _LOCAL_IMAGE_PREFIX
    revision = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{revision}"
        if registry else f"{prefix}-{component}:{revision}"
        for component in components
    }
    environment_references = dict(references)
    if build_metax_runtime:
        for name in ("metax-sdk", "vllm-metax"):
            environment_references[name] = (
                f"{prefix}/{name}:{revision}" if registry else f"{prefix}-{name}:{revision}"
            )
    if engines:
        environment_references["engine-source"] = references["model-server"] + "-engine"
    secret_names = tuple(secret["name"] for value in values for secret in value.get("imagePullSecrets", []))
    nodes = local_build_nodes(kubectl, command.registry, build.get("containerd_socket", ""))
    if checkout is not None:
        nodes = [(node, socket, f"{claim}-{binding[:12]}") for node, socket, claim in nodes]
    if registry:
        claim = find_build_cache(kubectl, namespace, binding, origin.node, "/var/cache/foretoken")
        node_uid = kubectl.get("node", origin.node)["metadata"]["uid"][:8]
        default_claim = "foretoken-application-compiler-" + node_uid
        if checkout is not None:
            default_claim += "-" + binding[:12]
        nodes = [(origin.node, "", claim or default_claim)]

    # Null inputs precede the first sync; absent legacy inputs remain unknown.
    # An interrupted installation never replaces the last committed source binding.
    state_directory.mkdir(parents=True, exist_ok=True)
    pending_state = {
        "root": str(root),
        "context": context_identity(kubectl),
        **({"inputs": pending.get("inputs")} if not pending or "inputs" in pending else {}),
        "base_image": inference_engine_image,
        "build": {
            "binding": binding,
            **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
            "caches": [
                *build.get("caches", []),
                *(cache for cache in pending.get("build", {}).get("caches", []) if cache not in build.get("caches", [])),
            ],
            "images": environment_references,
            "containerd_socket": nodes[0][1],
            "arguments": arguments,
            "registry_mirrors": registry_mirrors,
            "pull_secrets": list(secret_names),
        },
    }
    pending_state, execution_claims = _prepare_compiler_volumes(
        kubectl, namespace, origin, configuration, nodes, pending_path, pending_state,
        installing=checkout is None, timeout=command.timeout, cancellation=cancellation,
    )
    return _PreparedSourceBuild(
        previous_state=previous,
        previous_build=build,
        previous_inputs=old,
        compiler_configuration=configuration,
        pending_path=pending_path,
        pending_state=pending_state,
        nodes=nodes,
        execution_claims=execution_claims,
        registry=registry,
        revision=revision,
        mooncake_version=mooncake_version,
    )


def _select_build_arguments(
    command: InstallCommand,
    build: dict[str, Any],
    saved_arguments: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """Resolve build-source choices once, retaining the caller's explicit saved selections."""
    environment = os.environ.copy()
    if command.oci_registry:
        environment["FORETOKEN_OCI_REGISTRY"] = command.oci_registry
    registry_mirrors = build.get("registry_mirrors", {})
    if saved_arguments is None:
        selected = select_build_sources(
            environment,
            previous_environment=build.get("arguments"),
            previous_registry_mirrors=build.get("registry_mirrors"),
        )
        registry_mirrors = selected.registry_mirrors
        environment.update(selected.environment)
        for selection in selected.messages:
            print(f"Source mirror selected: {selection}", flush=True)
        arguments = build_arguments(environment)
    else:
        environment.update(saved_arguments)
        arguments = dict(saved_arguments)
    return arguments, registry_mirrors, environment


def _prepare_compiler_volumes(
    kubectl: Kubectl,
    namespace: str,
    origin: ApplicationFiles,
    configuration: dict[str, str],
    nodes: list[tuple[str, str, str]],
    pending_path: Path,
    pending_state: dict[str, Any],
    *,
    installing: bool,
    timeout: str,
    cancellation: CommandExecution,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Record each artifact/execution PVC before another allocation or cancellation can fail."""
    write_source_state(pending_path, pending_state)
    cancellation.check()
    settings = pending_state["build"]
    if installing:
        prepare_build_storage(
            kubectl, namespace, settings["arguments"], settings["registry_mirrors"],
            tuple(settings["pull_secrets"]), timeout,
        )
    pending_state = json.loads(pending_path.read_text())
    caches = pending_state["build"]["caches"]

    def record_cache(identity: dict[str, Any]) -> None:
        """Persist a volume UID before cancellation or another allocation can fail."""
        if identity not in caches:
            caches.append(identity)
            write_source_state(pending_path, pending_state)
        cancellation.check()

    execution_claims = {}
    for _, _, claim in nodes:
        cancellation.check()
        _, execution = ensure_build_volumes(
            kubectl, namespace, claim, configuration,
            binding=settings["binding"], timeout=timeout,
            record_identity=record_cache, recorded_caches=caches,
            owner={
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "name": origin.claim, "uid": origin.claim_uid,
            },
        )
        execution_claims[claim] = execution["name"] if execution is not None else ""
    return pending_state, execution_claims


def _prepare_workspaces(
    root: Path,
    prepared: _PreparedSourceBuild,
    snapshot: Path,
    contexts: ExitStack,
    kubectl: Kubectl,
    namespace: str,
    origin: ApplicationFiles,
    *,
    inference_engine_image: str | None,
    build_metax_runtime: bool,
    timeout: str,
    cancellation: CommandExecution,
) -> _SourceWorkspaces:
    """Adopt captured inputs into pending state, then start and synchronize owned compiler workspaces."""
    settings = prepared.pending_state["build"]
    directory = prepared.pending_path.parent
    with local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        snapshot = destination
        prepared.pending_state["inputs"] = snapshot.name
        write_source_state(prepared.pending_path, prepared.pending_state)
    base_image = (
        None if build_metax_runtime
        else resolve_vllm_base_image(snapshot, inference_engine_image, settings["arguments"].get("BASE_IMAGE_REGISTRY", ""))
    )
    credentials = registry_credentials(
        [
            prepared.compiler_configuration["image"], base_image or "",
            *(value for key, value in settings["arguments"].items() if key.endswith("_IMAGE")),
        ],
        registries=[
            prepared.registry, "docker.io", "gcr.io", "ghcr.io",
            *(endpoint for endpoints in settings["registry_mirrors"].values() for endpoint in endpoints),
            *(value for key, value in settings["arguments"].items() if key.endswith("REGISTRY")),
        ],
    )
    versions = snapshot_versions(snapshot, prepared.previous_inputs, prepared.previous_build.get("versions", {}))
    files = {str(path.relative_to(snapshot)): path for path in snapshot.rglob("*") if path.is_file()}
    builders = []
    for node, socket, claim in prepared.nodes:
        builder = contexts.enter_context(ClusterBuilder(
            kubectl, namespace, claim, "/var/cache/foretoken", prepared.compiler_configuration["image"],
            settings["binding"], timeout,
            tools_image=image_tools_image(settings["arguments"], settings["registry_mirrors"]),
            execution_claim=prepared.execution_claims[claim],
            node=node, containerd_socket=socket,
            pull_secrets=tuple(settings["pull_secrets"]), credentials=credentials,
            registry_mirrors=settings["registry_mirrors"], cancellation=cancellation,
        ))
        builders.append(builder)
        builder.sync(files, versions)
    publisher = next(builder for builder in builders if builder.node == origin.node)
    return _SourceWorkspaces(
        snapshot=snapshot, versions=versions, builders=builders,
        publisher=publisher, base_image=base_image,
    )


def _build_applications(
    engines: dict[str, str],
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    origin: ApplicationFiles,
    *,
    components: tuple[str, ...],
    runtime_backend: str,
    build_metax_runtime: bool,
    refresh: bool,
    cancellation: CommandExecution,
) -> _BuiltApplications:
    """Join component environment builds and file exports before any publisher consumes their cohort."""
    settings = prepared.pending_state["build"]
    arguments = settings["arguments"]
    build = prepared.previous_build
    previous = prepared.previous_state
    environments = BuildEnvironments(
        workspaces.builders, workspaces.versions,
        build.get("environments", {}) if build.get("registry") == prepared.registry else {},
        settings["images"], push=bool(prepared.registry), refresh=refresh,
    )
    publisher = workspaces.publisher
    changed = {
        path for path in workspaces.versions.keys() | build.get("versions", {}).keys()
        if path not in SNAPSHOT_METADATA and workspaces.versions.get(path) != build.get("versions", {}).get(path)
    }
    previous_revisions = {
        component: previous.get("bundles", {}).get(component, build.get("applications", {}).get(component, {})).get("revision", "")
        for component in components
    }
    application_parts: dict[str, list[str] | None] = {}
    engine_caches: dict[str, str] = {}
    engine_native = bool(engines) and (runtime_backend != "metax" or "vllm-metax" in engines)

    def record_engine_caches(caches: dict[str, str]) -> None:
        """Retain compiler identities before the engine dependency solve can fail."""
        engine_caches.update(caches)
        settings["engine_caches"] = dict(engine_caches)
        write_source_state(prepared.pending_path, prepared.pending_state)

    def build_component(component: str, dockerfile: str) -> dict[str, Any]:
        """Build a component's environment, then complete its application export at the origin."""
        engine_image = workspaces.base_image
        if component == "model-server" and build_metax_runtime:
            engine_image = build_metax_environment(environments, arguments)
        if component == "model-server":
            dependency_names = {
                "BASE_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE", "UV_DEFAULT_INDEX",
                "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_VLLM_PYTHON", "BUILDKIT_SYNTAX_IMAGE",
            }
        else:
            dependency_names = {
                "BUILDKIT_SYNTAX_IMAGE",
                "DISTROLESS_IMAGE_REGISTRY" if component == "control-plane" else "BASE_IMAGE_REGISTRY",
            }
        dependency_arguments = {key: value for key, value in arguments.items() if key in dependency_names}
        if component == "model-server" and engine_image:
            dependency_arguments["INFERENCE_ENGINE_IMAGE"] = engine_image
        selected = environments.build(
            component, dockerfile, target="environment", arguments=dependency_arguments,
            input_prefixes=("data-plane/patches/vllm/",) if component == "model-server" else (),
        )
        prior = previous_revisions[component]
        manifest = origin.read_manifest(origin.reference(component, prior)) if prior else None
        parts = None
        # A partial export can reuse only a completed application with the same
        # dependency base, compiler inputs and selected runtime environment.
        if (
            component != "control-plane" and manifest is not None
            and manifest.get("image") == previous.get("runtime", {}).get("model_image" if component == "model-server" else "image")
            and selected["image"] == build.get("images", {}).get("environment-" + component)
            and arguments == build.get("arguments")
        ):
            selected_parts = {"rust"} if component == "frontend" else {"rust", "python", "engine"}
            if not any(
                path in {".dockerignore", "LICENSE"}
                or (path.startswith("data-plane/") and not path.startswith("data-plane/model-server/python/"))
                for path in changed
            ):
                selected_parts.discard("rust")
            if not any(path.startswith("data-plane/model-server/python/") for path in changed):
                selected_parts.discard("python")
            parts = sorted(selected_parts)
        application_parts[component] = parts
        # Keep the export worker inside its owning cancellation/context scope,
        # including when engine environment preparation fails in parallel.
        with ThreadPoolExecutor(max_workers=1) as application_build, cancellation:
            payload = publisher.root + "/applications/" + component
            application = application_build.submit(
                cancellation.run_stage, publisher.export_application, component, payload, arguments, parts,
            )
            if component == "model-server" and engines:
                selected = build_engine_environment(
                    environments, arguments, runtime_image=selected["image"], base_image=engine_image,
                    cache_prefix=settings["engine_cache_scope"]["prefix"], record_caches=record_engine_caches,
                )
            application.result()
            if component == "model-server" and engines:
                export_engine_source(
                    publisher, payload, arguments,
                    runtime_image=selected["image"],
                    cache_id=engine_caches["" if prepared.registry else origin.node],
                    build_native=engine_native,
                )
            return selected

    with ThreadPoolExecutor(max_workers=len(set(components))) as builds, cancellation:
        pending = {
            component: builds.submit(cancellation.run_stage, build_component, component, dockerfile)
            for component, dockerfile in (
                ("control-plane", "control-plane/Dockerfile"),
                ("frontend", "data-plane/frontend/Dockerfile"),
                ("model-server", "data-plane/model-server/Dockerfile"),
            ) if component in components
        }
        selected = {component: future.result() for component, future in pending.items()}
    return _BuiltApplications(
        environments=environments,
        component_environments=selected,
        engine_caches=engine_caches,
        engine_native=engine_native,
        application_parts=application_parts,
        previous_revisions=previous_revisions,
    )


def _publish_applications(
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    built: _BuiltApplications,
    images: dict[str, str],
    origin: ApplicationFiles,
    *,
    checkout: Path | None,
    oci_registry: str | None,
    timeout: str,
) -> dict[str, str]:
    """Publish the completed source cohort while preserving checkout candidates and live consumers."""
    retained = None
    if checkout is not None:
        from foretoken.platform.config import default_platform_config
        from foretoken.platform.helm import Helm

        retained = origin.references(Helm(default_platform_config(oci_registry), origin.kubectl).application_history())
        if retained is not None:
            retained.add(prepared.revision)
            retained.update(
                bundle["revision"]
                for bundle in [
                    *prepared.previous_state.get("bundles", {}).values(),
                    *prepared.previous_build.get("applications", {}).values(),
                ]
            )
    publisher = workspaces.publisher
    publications = [
        ApplicationPublication(
            component=component,
            source=publisher.root + "/applications/" + component,
            revision=prepared.revision,
            previous=built.previous_revisions[component],
            sources=build_sources(workspaces.snapshot, component),
            image=image,
            parts=built.application_parts[component],
            previous_image=prepared.previous_state.get("runtime", {}).get("model_image" if component == "model-server" else "image"),
        )
        for component, image in images.items()
    ]
    revisions = origin.publish_batch(publisher, publications, retained, timeout=timeout)
    publisher.run(["rm", "-rf", "--", publisher.root + "/applications"], container=publisher.artifact_container)
    return revisions


def _deliver_environments(
    workspaces: _SourceWorkspaces,
    built: _BuiltApplications,
    images: dict[str, str],
    digests: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Deliver selected images after prerequisites, retaining borrowed environments and their OCI records."""
    for name, record in built.environments.records.items():
        for node, digest in record["digests"].items():
            digests[node]["environment-" + name] = digest
    environment_images = {"environment-" + name: record["image"] for name, record in built.environments.records.items()}
    for builder in workspaces.builders:
        built.environments.deliver(builder)
        builder.discard_unselected_images(set(images.values()) | set(environment_images.values()))
    return environment_images


def build_arguments(environment: dict[str, str]) -> dict[str, str]:
    """Map source mirror choices explicitly onto the existing image build interfaces."""
    result = {
        name: environment[name]
        for name in (
            "GOPROXY",
            "GOSUMDB",
            "FORETOKEN_GITHUB_MIRROR",
            "FORETOKEN_CARGO_REGISTRY",
            "CARGO_NET_GIT_FETCH_WITH_CLI",
            "UV_DEFAULT_INDEX",
            "UV_EXTRA_INDEX_URL",
            "UV_IMAGE",
            "METAX_SDK_IMAGE",
            "MACA_PATH",
            "MOONCAKE_VERSION",
            "UV_PYTHON",
            "FORETOKEN_VLLM_PYTHON",
        )
        if environment.get(name)
    }
    if environment.get("TORCH_CUDA_ARCH_LIST"):
        result["TARGET_CUDA_ARCH_LIST"] = environment["TORCH_CUDA_ARCH_LIST"]
    registry = environment.get("FORETOKEN_OCI_REGISTRY", "")
    docker = environment.get("FORETOKEN_DOCKER_IO_REGISTRY", registry).rstrip("/")
    ghcr = environment.get("FORETOKEN_GHCR_REGISTRY", registry).rstrip("/")
    gcr = environment.get("FORETOKEN_GCR_REGISTRY", registry).rstrip("/")
    if docker:
        result.update(
            BASE_IMAGE_REGISTRY=docker,
            GO_IMAGE_REGISTRY=docker,
            BUILDKIT_SYNTAX_IMAGE=f"{docker}/docker/dockerfile:1",
        )
    if ghcr:
        result["UV_IMAGE_REGISTRY"] = ghcr
    if gcr:
        result["DISTROLESS_IMAGE_REGISTRY"] = gcr
    return result
