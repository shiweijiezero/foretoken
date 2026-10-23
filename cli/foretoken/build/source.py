# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compose source environments and publish their completed applications."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from loguru import logger

from foretoken.applications.files import (
    ApplicationFiles,
    ApplicationPublication,
    PublishedApplications,
)
from foretoken.arguments import InstallCommand
from foretoken.build.cluster.builder import ClusterBuilder, registry_credentials
from foretoken.build.cluster.compiler import APPLICATION_PARTS, CompilerWorkspace
from foretoken.build.cluster.images import image_tools_image
from foretoken.build.cluster.storage import (
    build_configuration,
    ensure_build_volumes,
    ensure_shared_build_cache,
    local_build_nodes,
    prepare_build_storage,
)
from foretoken.build.engine import (
    build_engine_environment,
    build_metax_environment,
    engine_cache_scope,
    engine_native_cache_matches,
    resolve_vllm_base_image,
)
from foretoken.build.environments import BuildEnvironments
from foretoken.build.inputs import (
    SNAPSHOT_METADATA,
    BuildInputs,
    build_sources,
    engine_source_roots,
    mooncake_build_version,
    snapshot_versions,
    source_checkout,
)
from foretoken.build.records import source_build_record
from foretoken.build.state import (
    capture_build_inputs,
    checkout_operation,
    checkout_state_directory,
    cluster_state_directory,
    context_identity,
    local_candidates,
    write_source_state,
)
from foretoken.downloads.sources import select_build_sources
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.processes.execution import CommandExecution

_LOCAL_IMAGE_PREFIX = "docker.io/library/foretoken-dev"
_COMPONENT_DOCKERFILES = {
    "control-plane": "control-plane/Dockerfile",
    "frontend": "data-plane/frontend/Dockerfile",
    "model-server": "data-plane/model-server/Dockerfile",
}


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
    """Separate completed applications, reusable environments and recoverable inputs before execution."""

    engines: dict[str, str]
    previous_state: dict[str, Any]
    previous_build: dict[str, Any]
    cached_build: dict[str, Any]
    previous_inputs: Path | None
    previous_versions: dict[str, str]
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
    """Captured inputs and image builders whose storage also hosts the operation's native compiler."""

    snapshot: Path
    files: dict[str, Path]
    versions: dict[str, str]
    builders: list[ClusterBuilder]
    origin_builder: ClusterBuilder
    base_image: str | None
    credentials: dict[str, Any]


@dataclass(frozen=True)
class _BuiltEnvironments:
    """Completed runtime and toolchain images with the application parts selected for native compilation."""

    environments: BuildEnvironments
    component_environments: dict[str, dict[str, Any]]
    compilers: dict[str, str]
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
    """Prepare a source directory's reusable environments and publish its native applications.

    Installers acquire the workspace lock; deployment callers retain their existing
    lock. Dependencies finish before image delivery, and compiler processes exit
    before the caller commits the completed selection.
    """
    cancellation = cancellation or CommandExecution.current() or CommandExecution(timeout_seconds(command.timeout))
    cancellation.check()
    root = source_checkout(command.editable)
    kubectl = origin.kubectl
    directory = checkout or checkout_state_directory(
        cluster_state_directory(kubectl), root, command.timeout, context=context_identity(kubectl),
    )
    operation = checkout_operation(directory, kubectl, command.timeout) if checkout is None else nullcontext()
    with operation:
        prepared = _prepare_source_build(
            root, command, kubectl, namespace, values, origin,
            inference_engine_image=inference_engine_image,
            build_metax_runtime=build_metax_runtime, runtime_backend=runtime_backend,
            saved_arguments=saved_arguments, directory=directory, installing=checkout is None,
            components=components, cancellation=cancellation,
        )
        engines = prepared.engines
        with capture_build_inputs(
            root, engines, prepared.previous_inputs, mooncake_version=prepared.mooncake_version,
        ) as snapshot, ExitStack() as build_contexts:
            workspaces = _prepare_workspaces(
                prepared, snapshot, build_contexts, kubectl, namespace, origin,
                inference_engine_image=inference_engine_image,
                build_metax_runtime=build_metax_runtime,
                timeout=command.timeout, cancellation=cancellation,
            )
            # Compile against delivered toolchains while runtime environments are prepared.
            # Keep the compiler and captured inputs alive through both publications.
            with _build_applications(
                prepared, workspaces, origin, command.timeout,
                components=components, runtime_backend=runtime_backend,
                build_metax_runtime=build_metax_runtime, refresh=checkout is None,
                before_delivery=before_delivery,
            ) as (built, compiler):
                images = {component: built.component_environments[component]["image"] for component in components}
                BuildInputs.select(root, engines, mooncake_version=prepared.mooncake_version).validate(workspaces.snapshot)
                published = _publish_applications(
                    prepared, workspaces, built, images, compiler, origin,
                    checkout=checkout, oci_registry=command.oci_registry, timeout=command.timeout,
                )
                revisions = published.revisions
                applications = {
                    component: origin.reference(component, revision)
                    for component, revision in revisions.items()
                }
                BuildInputs.select(root, engines, mooncake_version=prepared.mooncake_version).validate(workspaces.snapshot)
                build_state = prepared.pending_state["build"]
                build_state["applications"] = {
                    component: {"revision": revision} for component, revision in revisions.items()
                }
                if published.completed_build is not None:
                    build_state["completed"] = published.completed_build
                write_source_state(prepared.pending_path, prepared.pending_state)
            build_contexts.close()
            yield SourceImages(
                root, "registry" if prepared.registry else "import",
                images.get("control-plane", ""), images["frontend"], images["model-server"],
                workspaces.snapshot, build_state, applications,
            )


def _prepare_source_build(
    root: Path,
    command: InstallCommand,
    kubectl: Kubectl,
    namespace: str,
    values: Sequence[dict[str, Any]],
    origin: ApplicationFiles,
    *,
    inference_engine_image: str | None,
    build_metax_runtime: bool,
    runtime_backend: str,
    saved_arguments: dict[str, str] | None,
    directory: Path,
    installing: bool,
    components: tuple[str, ...],
    cancellation: CommandExecution,
) -> _PreparedSourceBuild:
    """Resolve completed and interrupted build history, then record owned volumes before execution."""
    state_path = directory / "install.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    build = previous.get("build", {}) if previous.get("root") == str(root) else {}
    pending_path = directory / "build.json"
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    if not build and pending.get("root") == str(root):
        build = pending["build"]
    binding = directory.name
    selection = pending if pending.get("root") == str(root) and "engines" in pending else previous
    engines = (
        engine_source_roots(command.engine_sources)
        if command.engine_sources else selection.get("engines", {})
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
    arguments, registry_mirrors = _select_build_arguments(command, build, saved_arguments)
    saved_inputs = (
        pending
        if pending.get("root") == str(root) and pending.get("inputs") and pending.get("build", {}).get("versions")
        else previous
    )
    old = directory / saved_inputs["inputs"] if saved_inputs.get("root") == str(root) and saved_inputs.get("inputs") else None
    previous_versions = saved_inputs.get("build", {}).get("versions", {}) if old is not None else {}
    mooncake_version = mooncake_build_version(
        root, arguments, runtime_backend=runtime_backend, build_metax_runtime=build_metax_runtime,
    )
    configuration = build_configuration(
        root, values,
        arguments.get("BASE_IMAGE_REGISTRY", registry_mirrors.get("docker.io", [""])[0]),
    )
    registry = (command.registry or "").rstrip("/")
    cached_build = (
        pending["build"]
        if pending.get("root") == str(root) and pending.get("build", {}).get("binding") == binding
        and pending["build"].get("registry") == registry and pending["build"].get("environments")
        else build
    )
    cached_environments = (
        cached_build.get("environments", {}) if cached_build.get("registry") == registry else {}
    )
    prefix = registry if registry else _LOCAL_IMAGE_PREFIX
    revision = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{revision}"
        if registry else f"{prefix}-{component}:{revision}"
        for component in components
    }
    environment_references = {
        **references,
        **{"compiler-" + component: reference + "-compiler" for component, reference in references.items()},
    }
    if build_metax_runtime:
        for name in ("metax-sdk", "vllm-metax"):
            environment_references[name] = (
                f"{prefix}/{name}:{revision}" if registry else f"{prefix}-{name}:{revision}"
            )
    if engines:
        environment_references["engine-source"] = references["model-server"] + "-engine"
        environment_references["compiler-engine"] = references["model-server"] + "-engine-compiler"
    secret_names = tuple(secret["name"] for value in values for secret in value.get("imagePullSecrets", []))
    nodes = local_build_nodes(kubectl, command.registry, build.get("containerd_socket", ""))
    if registry:
        node_uid = kubectl.get("node", origin.node)["metadata"]["uid"][:8]
        nodes = [(origin.node, "", "foretoken-application-compiler-" + node_uid)]
    nodes = [(node, socket, f"{claim}-{binding[:12]}") for node, socket, claim in nodes]

    # Retain recovery inputs, images and native-cache identities before storage
    # preparation can fail, without replacing the last completed installation.
    directory.mkdir(parents=True, exist_ok=True)
    pending_state = {
        "root": str(root),
        "engines": engines,
        "context": context_identity(kubectl),
        "inputs": old.name if old is not None else None,
        "base_image": inference_engine_image,
        "build": {
            "binding": binding,
            "configuration": configuration,
            "registry": registry,
            "runtime_backend": runtime_backend,
            "build_metax_runtime": build_metax_runtime,
            **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
            "caches": [
                *build.get("caches", []),
                *(cache for cache in pending.get("build", {}).get("caches", []) if cache not in build.get("caches", [])),
            ],
            "images": {
                **environment_references,
                **{"environment-" + name: record["image"] for name, record in cached_environments.items()},
            },
            "environments": cached_environments,
            "compilers": cached_build.get("compilers", {}) if cached_environments else {},
            "engine_caches": (
                dict(cached_build.get("engine_caches", {}))
                if cached_environments and cached_build.get("engine_cache_scope") == cache_scope else {}
            ),
            "versions": previous_versions,
            "containerd_socket": nodes[0][1],
            "arguments": arguments,
            "registry_mirrors": registry_mirrors,
            "pull_secrets": list(secret_names),
        },
    }
    pending_state, execution_claims = _prepare_compiler_volumes(
        kubectl, namespace, origin, configuration, nodes, pending_path, pending_state,
        installing=installing, timeout=command.timeout, cancellation=cancellation,
    )
    return _PreparedSourceBuild(
        engines=engines, previous_state=previous, previous_build=build, cached_build=cached_build,
        previous_inputs=old, previous_versions=previous_versions,
        compiler_configuration=configuration, pending_path=pending_path, pending_state=pending_state,
        nodes=nodes, execution_claims=execution_claims, registry=registry,
        revision=revision, mooncake_version=mooncake_version,
    )


def _select_build_arguments(
    command: InstallCommand,
    build: dict[str, Any],
    saved_arguments: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Resolve download and compilation choices once, retaining explicit saved selections."""
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
            logger.info("Source mirror selected: {}", selection)
        arguments = build_arguments(environment)
    else:
        arguments = dict(saved_arguments)
    return arguments, registry_mirrors


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
    """Capture input versions before retiring older snapshots, then synchronize isolated image builders."""
    settings = prepared.pending_state["build"]
    versions = snapshot_versions(snapshot, prepared.previous_inputs, prepared.previous_versions)
    directory = prepared.pending_path.parent
    with local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        snapshot = destination
        prepared.pending_state["inputs"] = snapshot.name
        settings["versions"] = versions
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
    files = {str(path.relative_to(snapshot)): path for path in snapshot.rglob("*") if path.is_file()}
    builders = []
    for node, socket, claim in prepared.nodes:
        client_cache = ensure_shared_build_cache(
            kubectl, namespace, node, prepared.compiler_configuration,
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": origin.claim, "uid": origin.claim_uid},
            role="node-client",
        ) if socket else ""
        builder = contexts.enter_context(ClusterBuilder(
            kubectl, namespace, claim, "/var/cache/foretoken", prepared.compiler_configuration["image"],
            settings["binding"], timeout,
            tools_image=image_tools_image(settings["arguments"], settings["registry_mirrors"]),
            execution_claim=prepared.execution_claims[claim],
            client_cache_claim=client_cache, client_build_arguments=settings["arguments"],
            node=node, containerd_socket=socket,
            pull_secrets=tuple(settings["pull_secrets"]), credentials=credentials,
            registry_mirrors=settings["registry_mirrors"], cancellation=cancellation,
        ))
        builders.append(builder)
        builder.sync(files, versions)
    return _SourceWorkspaces(
        snapshot=snapshot, files=files, versions=versions, builders=builders,
        origin_builder=next(builder for builder in builders if builder.node == origin.node),
        base_image=base_image, credentials=credentials,
    )


def _build_compilers(
    environments: BuildEnvironments, arguments: dict[str, str], components: tuple[str, ...],
    builder: ClusterBuilder,
) -> dict[str, str]:
    """Prepare first-party toolchains on the application origin independently of runtime dependencies."""
    cancellation = builder.cancellation

    def build_component(component: str) -> str:
        """Build the component's toolchain target without resolving a serving image."""
        compiler_arguments = {key: value for key, value in arguments.items() if key in {
            "BASE_IMAGE_REGISTRY", "GO_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE",
            "GO_BUILDER_IMAGE", "RUST_BUILDER_IMAGE", "BUILDKIT_SYNTAX_IMAGE", "FORETOKEN_GITHUB_MIRROR",
        }}
        if component == "model-server":
            # Dockerfile parsing also resolves FROM arguments in unselected runtime stages.
            compiler_arguments["INFERENCE_ENGINE_IMAGE"] = "scratch"
        compiler = environments.build(
            "compiler-" + component, _COMPONENT_DOCKERFILES[component], target="compiler",
            arguments=compiler_arguments, input_prefixes=(), execution_builder=builder,
        )
        return compiler["image"]

    with ThreadPoolExecutor(max_workers=len(components)) as builds, cancellation:
        pending = {
            component: builds.submit(cancellation.run_stage, build_component, component)
            for component in components
        }
        return {component: future.result() for component, future in pending.items()}


def _select_application_parts(
    prepared: _PreparedSourceBuild, workspaces: _SourceWorkspaces, origin: ApplicationFiles,
    components: tuple[str, ...],
) -> tuple[dict[str, str], dict[str, list[str] | None]]:
    """Select source-required work from completed publications before runtime compatibility is known."""
    previous, build = prepared.previous_state, prepared.previous_build
    changed = {
        path for path in workspaces.versions.keys() | build.get("versions", {}).keys()
        if path not in SNAPSHOT_METADATA and workspaces.versions.get(path) != build.get("versions", {}).get(path)
    }
    revisions = {
        component: previous.get("bundles", {}).get(component, build.get("applications", {}).get(component, {})).get("revision", "")
        for component in components
    }
    parts: dict[str, list[str] | None] = {}
    for component, revision in revisions.items():
        manifest = origin.read_manifest(origin.reference(component, revision)) if revision else None
        parts[component] = None
        if (
            component == "control-plane" or manifest is None
            or manifest.get("image") != previous.get("runtime", {}).get("model_image" if component == "model-server" else "image")
            or prepared.pending_state["build"]["arguments"] != build.get("arguments")
        ):
            continue
        selected = set(APPLICATION_PARTS[component])
        if not any(
            path in {".dockerignore", "LICENSE", "deploy/build-source"}
            or (path.startswith("data-plane/") and not path.startswith("data-plane/model-server/python/"))
            for path in changed
        ):
            selected.discard("rust")
        if not any(path.startswith("data-plane/model-server/python/") for path in changed):
            selected.discard("python")
        parts[component] = sorted(selected)
    return revisions, parts


def _build_environments(
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    environments: BuildEnvironments,
    previous_revisions: dict[str, str],
    application_parts: dict[str, list[str] | None],
    *,
    runtime_backend: str,
    build_metax_runtime: bool,
) -> _BuiltEnvironments:
    """Prepare runtimes and the ABI-dependent engine toolchain, then finalize publication compatibility."""
    settings = prepared.pending_state["build"]
    arguments = settings["arguments"]
    build, cached = prepared.previous_build, prepared.cached_build
    compilers: dict[str, str] = {}
    application_parts = dict(application_parts)
    engine_caches: dict[str, str] = {}
    engine_native = bool(prepared.engines) and (runtime_backend != "metax" or "vllm-metax" in prepared.engines)

    def build_component(component: str) -> dict[str, Any]:
        """Resolve one runtime and any source-engine dependencies in its exact ABI environment."""
        engine_image = workspaces.base_image
        if component == "model-server" and build_metax_runtime:
            engine_image = build_metax_environment(environments, arguments)
        if component == "model-server":
            dependency_names = {
                "BASE_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE", "UV_DEFAULT_INDEX",
                "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_VLLM_PYTHON", "BUILDKIT_SYNTAX_IMAGE", "GOPROXY", "GOSUMDB",
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
            component, _COMPONENT_DOCKERFILES[component], target="environment", arguments=dependency_arguments,
            input_prefixes=("data-plane/patches/vllm/", "deploy/mooncake/", "third_party/mooncake/") if component == "model-server" else (),
        )
        if selected["image"] != build.get("images", {}).get("environment-" + component):
            application_parts[component] = None
        if component == "model-server" and prepared.engines:
            selected, compiler_image = build_engine_environment(
                environments, arguments, runtime_image=selected["image"], base_image=engine_image,
                cache_prefix=settings["engine_cache_scope"]["prefix"],
                compiler_builder=workspaces.origin_builder,
            )
            compilers["engine"] = compiler_image
            for builder in workspaces.builders:
                key = "" if prepared.registry else builder.node
                previous_cache = cached.get("engine_caches", {}).get(key)
                engine_caches[key] = (
                    previous_cache
                    if previous_cache and engine_native_cache_matches(
                        cached, environments.records, settings["engine_cache_scope"], key,
                    )
                    else settings["binding"] + "-" + uuid.uuid4().hex
                )
        return selected

    cancellation = workspaces.origin_builder.cancellation
    with ThreadPoolExecutor(max_workers=len(previous_revisions)) as builds, cancellation:
        pending = {
            component: builds.submit(cancellation.run_stage, build_component, component)
            for component in previous_revisions
        }
        selected = {component: future.result() for component, future in pending.items()}
    return _BuiltEnvironments(
        environments=environments, component_environments=selected, compilers=compilers,
        engine_caches=engine_caches, engine_native=engine_native,
        application_parts=application_parts, previous_revisions=previous_revisions,
    )


def _deliver_environments(
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    built: _BuiltEnvironments,
    images: dict[str, str],
) -> None:
    """Deliver completed environments across nodes, then persist their reusable image selections."""
    digests: dict[str, dict[str, str]] = {builder.node: {} for builder in workspaces.builders}
    for component, record in built.component_environments.items():
        for node, digest in record["digests"].items():
            digests[node][component] = digest
    for name, record in built.environments.records.items():
        for node, digest in record["digests"].items():
            digests[node]["environment-" + name] = digest
    environment_images = {"environment-" + name: record["image"] for name, record in built.environments.records.items()}
    cancellation = workspaces.origin_builder.cancellation
    # Each node owns its image index and store. Imports stay serial within a node,
    # while independent nodes receive their completed environments concurrently.
    with ThreadPoolExecutor(max_workers=len(workspaces.builders)) as deliveries, cancellation:
        pending = [
            deliveries.submit(cancellation.run_stage, built.environments.deliver, builder)
            for builder in workspaces.builders
        ]
        for future in pending:
            future.result()
    prepared.pending_state["build"].update(
        digests=digests, images={**images, **environment_images},
        environments=built.environments.records, compilers=built.compilers,
        engine_caches=built.engine_caches, engine_native=built.engine_native,
    )
    write_source_state(prepared.pending_path, prepared.pending_state)


@contextmanager
def _build_applications(
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    origin: ApplicationFiles,
    timeout: str,
    *,
    components: tuple[str, ...],
    runtime_backend: str,
    build_metax_runtime: bool,
    refresh: bool,
    before_delivery: Callable[[], None] | None,
) -> Iterator[tuple[_BuiltEnvironments, CompilerWorkspace]]:
    """Start independent runtimes and toolchains together; retain writers through delivery and publication."""
    settings = prepared.pending_state["build"]
    builder = workspaces.origin_builder
    cancellation = builder.cancellation
    environments = BuildEnvironments(
        workspaces.builders, workspaces.versions, settings["environments"],
        settings["images"], push=bool(prepared.registry), refresh=refresh,
    )
    previous_revisions, early_parts = _select_application_parts(prepared, workspaces, origin, components)
    # Keep acquired compiler resources outside the worker scope: cancellation
    # stops and joins every stage before its workspace can be released.
    with ExitStack() as resources:
        with ThreadPoolExecutor(max_workers=len(components) + 1) as builds, cancellation:
            # Runtime dependencies do not depend on the Go/Rust toolchains.
            runtime = builds.submit(
                cancellation.run_stage, _build_environments,
                prepared, workspaces, environments, previous_revisions, early_parts,
                runtime_backend=runtime_backend, build_metax_runtime=build_metax_runtime,
            )
            compilers = _build_compilers(environments, settings["arguments"], components, builder)
            compiler_names = {"compiler-" + component for component in components}
            environments.deliver(builder, compiler_names)
            # Runtime identities and their native cache selections must be saved
            # together; only these delivered toolchains belong to this checkpoint.
            settings.update(
                environments={
                    **environments.previous,
                    **{name: record for name, record in environments.records.items() if name in compiler_names},
                },
                compilers={**settings["compilers"], **compilers},
            )
            settings["images"].update({"environment-" + name: record["image"] for name, record in settings["environments"].items()})
            write_source_state(prepared.pending_path, prepared.pending_state)
            compiler = resources.enter_context(CompilerWorkspace(
                origin.kubectl, origin.namespace, builder.claim, settings["binding"], origin.node,
                compilers, prepared.compiler_configuration, timeout,
                tools_image=image_tools_image(settings["arguments"], settings["registry_mirrors"]),
                execution_claim=builder.execution_claim,
                owner={"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": origin.claim, "uid": origin.claim_uid},
                pull_secrets=tuple(settings["pull_secrets"]), credentials=workspaces.credentials,
                cancellation=cancellation,
            ))
            compiler.sync(workspaces.files, workspaces.versions)

            def compile_application(component: str) -> None:
                """Compile source-required parts first, then fill any runtime-invalidated parts."""
                parts = early_parts[component]
                compiled = set(APPLICATION_PARTS[component] if parts is None else parts) - {"engine"}
                compiler.compile_application(component, settings["arguments"], sorted(compiled))
                parts = runtime.result().application_parts[component]
                required = set(APPLICATION_PARTS[component] if parts is None else parts) - {"engine"}
                if remaining := required - compiled:
                    compiler.compile_application(component, settings["arguments"], sorted(remaining))

            pending = [
                builds.submit(cancellation.run_stage, compile_application, component)
                for component in components
            ]
            built = runtime.result()
            built = replace(built, compilers={**compilers, **built.compilers})
            if "engine" in built.compilers:
                environments.deliver(builder, {"compiler-engine"})
                settings.update(
                    environments=dict(environments.records), compilers=built.compilers,
                    engine_caches=built.engine_caches, engine_native=built.engine_native,
                )
                settings["images"].update({"environment-" + name: record["image"] for name, record in environments.records.items()})
                write_source_state(prepared.pending_path, prepared.pending_state)
                pending.append(builds.submit(
                    cancellation.run_stage, compiler.compile_engine,
                    built.compilers["engine"], settings["arguments"],
                    cache_id=built.engine_caches["" if prepared.registry else origin.node],
                    build_native=built.engine_native,
                ))
            # Environment dependency writes have finished. Native compilers use
            # separate directories and can continue during node image delivery.
            if before_delivery is not None:
                before_delivery()
            images = {component: record["image"] for component, record in built.component_environments.items()}
            _deliver_environments(prepared, workspaces, built, images)
            for future in pending:
                future.result()

        for component in components:
            compiler.export_application(
                component, compiler.root + "/applications/" + component,
                engine=component == "model-server" and "engine" in built.compilers,
            )
        yield built, compiler


def _publish_applications(
    prepared: _PreparedSourceBuild,
    workspaces: _SourceWorkspaces,
    built: _BuiltEnvironments,
    images: dict[str, str],
    compiler: CompilerWorkspace,
    origin: ApplicationFiles,
    *,
    checkout: Path | None,
    oci_registry: str | None,
    timeout: str,
) -> PublishedApplications:
    """Publish completed exports together, retaining live consumers and the checkout's prior selections."""
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
    publications = [
        ApplicationPublication(
            component=component,
            source=compiler.root + "/applications/" + component,
            revision=prepared.revision,
            previous=built.previous_revisions[component],
            sources=build_sources(workspaces.snapshot, component),
            image=image,
            parts=built.application_parts[component],
            previous_image=prepared.previous_state.get("runtime", {}).get("model_image" if component == "model-server" else "image"),
        )
        for component, image in images.items()
    ]
    record = source_build_record(prepared.pending_state["build"]) if checkout is None else None
    published = origin.publish_batch(compiler, publications, retained, timeout=timeout, build_record=record)
    compiler.run(["rm", "-rf", "--", compiler.root + "/applications"], container=compiler.artifact_container)
    return published


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
