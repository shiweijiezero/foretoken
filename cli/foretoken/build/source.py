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

from foretoken.applications.files import ApplicationFiles
from foretoken.arguments import InstallCommand
from foretoken.build.cluster import (
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
)
from foretoken.build.engine import (
    engine_build_arguments,
    engine_cache_scope,
    export_engine_source,
    resolve_vllm_base_image,
)
from foretoken.build.environments import BuildEnvironments
from foretoken.build.inputs import (
    SNAPSHOT_METADATA,
    BuildInputs,
    build_sources,
    engine_source_roots,
    mooncake_inputs,
    pinned_rust_revision,
    snapshot_versions,
    source_checkout,
)
from foretoken.build.node_images import image_tools_image
from foretoken.build.state import (
    capture_build_inputs,
    cluster_state_directory,
    context_identity,
    local_candidates,
    write_source_state,
)
from foretoken.build.storage import (
    build_configuration,
    ensure_build_volumes,
    local_build_nodes,
    prepare_build_storage,
)
from foretoken.command_execution import CommandExecution
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.network_sources import select_build_sources


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
    kubectl = Kubectl()
    engines = engine_source_roots(command.engine_sources)
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
    # Save compiler identity before any stage can fail; recipes are not cache namespaces.
    cache_scope = None
    if engines:
        pending_build = pending.get("build", {}) if pending.get("root") == str(root) and pending.get("build", {}).get("binding") == binding else {}
        saved_scope = pending_build.get("engine_cache_scope") or build.get("engine_cache_scope")
        cache_scope = engine_cache_scope(binding, engines, saved_scope)
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
    old = state_directory / previous["inputs"] if previous.get("inputs") else None
    mooncake_version = arguments.get("MOONCAKE_VERSION", "") if build_metax_runtime else None
    if mooncake_version is not None:
        _, dependency = mooncake_inputs(root, old, mooncake_version)
        if not dependency["prepared"]:
            prepared = cancellation.run(
                [str(root / "deploy/mooncake/prepare-source")],
                cwd=root,
                env=environment,
            )
            if prepared.returncode:
                raise DeploymentError(
                    "could not prepare the MetaX runtime's Mooncake source"
                )
    arguments["VLLM_REVISION"] = pinned_rust_revision(root)
    configuration = build_configuration(
        root,
        values,
        arguments.get(
            "BASE_IMAGE_REGISTRY", registry_mirrors.get("docker.io", [""])[0]
        ),
    )
    registry = (command.registry or "").rstrip("/")
    prefix = registry if registry else "docker.io/library/foretoken-dev"
    suffix = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{suffix}"
        if registry
        else f"{prefix}-{component}:{suffix}"
        for component in components
    }
    environment_references = dict(references)
    if build_metax_runtime:
        for name in ("metax-sdk", "vllm-metax"):
            environment_references[name] = (
                f"{prefix}/{name}:{suffix}" if registry else f"{prefix}-{name}:{suffix}"
            )
    if engines:
        environment_references["engine-source"] = references["model-server"] + "-engine"
    secret_names = tuple(
        secret["name"]
        for value in values
        for secret in value.get("imagePullSecrets", [])
    )
    nodes = local_build_nodes(
        kubectl, command.registry, build.get("containerd_socket", "")
    )
    if checkout is not None:
        nodes = [(node, socket, f"{claim}-{binding[:12]}") for node, socket, claim in nodes]
    if registry:
        claim = find_build_cache(
            kubectl, namespace, binding, origin.node, "/var/cache/foretoken"
        )
        node_uid = kubectl.get("node", origin.node)["metadata"]["uid"][:8]
        default_claim = "foretoken-application-compiler-" + node_uid
        if checkout is not None:
            default_claim += "-" + binding[:12]
        nodes = [(origin.node, "", claim or default_claim)]
    # Keep a failed first installation's compiler cache addressable for retry and
    # uninstall, without replacing an existing successful installation binding.
    # Null inputs precede the first sync; absent inputs in legacy pending state are unknown.
    state_directory.mkdir(parents=True, exist_ok=True)
    write_source_state(
        pending_path,
        {
            "root": str(root),
            "context": context_identity(kubectl),
            **({"inputs": pending.get("inputs")} if not pending or "inputs" in pending else {}),
            "base_image": inference_engine_image,
            "build": {
                "binding": binding,
                **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
                "caches": [
                    *build.get("caches", []),
                    *(cache for cache in pending.get("build", {}).get("caches", [])
                      if cache not in build.get("caches", [])),
                ],
                "images": environment_references,
                "containerd_socket": nodes[0][1],
                "arguments": arguments,
                "registry_mirrors": registry_mirrors,
                "pull_secrets": list(secret_names),
            },
        },
    )
    cancellation.check()
    if checkout is None:
        prepare_build_storage(
            kubectl, namespace, arguments,
            registry_mirrors, secret_names, command.timeout,
        )
    pending_state = json.loads(pending_path.read_text())
    caches = pending_state["build"]["caches"]

    def record_cache(identity: dict[str, Any]) -> None:
        """Persist each created volume before cancellation or another allocation can fail."""
        if identity not in caches:
            caches.append(identity)
            write_source_state(pending_path, pending_state)
        cancellation.check()

    execution_claims = {}
    for _, _, claim in nodes:
        cancellation.check()
        _, execution = ensure_build_volumes(
            kubectl, namespace, claim, configuration,
            binding=binding, timeout=command.timeout, record_identity=record_cache,
            recorded_caches=caches,
            owner={
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "name": origin.claim, "uid": origin.claim_uid,
            },
        )
        execution_claims[claim] = execution["name"] if execution is not None else ""
    with capture_build_inputs(
        root, engines, old, mooncake_version=mooncake_version,
    ) as snapshot, ExitStack() as build_contexts:
        # Pending inputs survive a failed build: compiler storage is never their only copy.
        with local_candidates(state_directory):
            destination = state_directory / snapshot.name
            snapshot.rename(destination)
            snapshot = destination
            pending_state["inputs"] = snapshot.name
            write_source_state(pending_path, pending_state)
        # Resolve automatic defaults from the same inputs used by the build, without
        # recording them as an explicit base selection in the installation binding.
        base_image = (
            None
            if build_metax_runtime
            else resolve_vllm_base_image(
                snapshot,
                inference_engine_image,
                arguments.get("BASE_IMAGE_REGISTRY", ""),
            )
        )
        credentials = registry_credentials(
            [
                configuration["image"],
                prefix,
                base_image or "",
                "docker.io",
                "gcr.io",
                "ghcr.io",
                *(
                    endpoint
                    for endpoints in registry_mirrors.values()
                    for endpoint in endpoints
                ),
                *(
                    value
                    for key, value in arguments.items()
                    if key.endswith(("REGISTRY", "_IMAGE"))
                ),
            ]
        )
        versions = snapshot_versions(snapshot, old, build.get("versions", {}))
        files = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        engine_caches: dict[str, str] = {}
        engine_native = bool(engines) and (
            runtime_backend != "metax" or "vllm-metax" in engines
        )
        builders = []
        for node, socket, claim in nodes:
            builder = build_contexts.enter_context(
                ClusterBuilder(
                    kubectl,
                    namespace,
                    claim,
                    "/var/cache/foretoken",
                    configuration["image"],
                    binding,
                    command.timeout,
                    tools_image=image_tools_image(arguments, registry_mirrors),
                    execution_claim=execution_claims[claim],
                    node=node,
                    containerd_socket=socket,
                    pull_secrets=secret_names,
                    credentials=credentials,
                    registry_mirrors=registry_mirrors,
                    cancellation=cancellation,
                )
            )
            builders.append(builder)
            builder.sync(files, versions)

        environments = BuildEnvironments(
            builders, versions,
            build.get("environments", {}) if build.get("registry") == registry else {},
            environment_references, push=bool(registry), refresh=checkout is None,
        )
        publisher = next(builder for builder in builders if builder.node == origin.node)
        changed = {
            path for path in versions.keys() | build.get("versions", {}).keys()
            if path not in SNAPSHOT_METADATA
            and versions.get(path) != build.get("versions", {}).get(path)
        }
        prior_revisions = {
            component: previous.get("bundles", {})
            .get(component, build.get("applications", {}).get(component, {}))
            .get("revision", "")
            for component in components
        }
        application_parts: dict[str, list[str] | None] = {}

        def build_component(component: str, dockerfile: str) -> dict[str, Any]:
            """Prepare each environment once per node, then export its application at the origin."""
            engine_image = base_image
            if component == "model-server" and build_metax_runtime:
                runtime_arguments = {
                    key: value for key, value in arguments.items()
                    if key in {
                        "METAX_SDK_IMAGE", "UV_IMAGE_REGISTRY", "UV_IMAGE", "MACA_PATH", "UV_PYTHON",
                        "FORETOKEN_GITHUB_MIRROR", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
                        "BUILDKIT_SYNTAX_IMAGE",
                    }
                }
                if not runtime_arguments.get("METAX_SDK_IMAGE"):
                    sdk = environments.build(
                        "metax-sdk", "deploy/inference-engines/vllm-metax/Dockerfile",
                        target="metax-sdk",
                        arguments={
                            key: value for key, value in arguments.items()
                            if key in {
                                "BASE_IMAGE_REGISTRY", "UV_IMAGE_REGISTRY", "UV_IMAGE",
                                "BUILDKIT_SYNTAX_IMAGE", "FORETOKEN_GITHUB_MIRROR",
                                "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
                            }
                        },
                        input_prefixes=(
                            "deploy/inference-engines/vllm-metax/prepare-sdk.sh",
                            "deploy/inference-engines/vllm-metax/torch_cpu_config.py",
                            "deploy/inference-engines/vllm-metax/sdk-environment.json",
                        ),
                    )
                    runtime_arguments["METAX_SDK_IMAGE"] = sdk["image"]
                runtime = environments.build(
                    "vllm-metax", "deploy/inference-engines/vllm-metax/Dockerfile",
                    arguments=runtime_arguments,
                    input_prefixes=(
                        "deploy/inference-engines/vllm-metax/install.sh",
                        "deploy/inference-engines/vllm-metax/sdk_audio.py",
                        "deploy/inference-engines/vllm-metax/source-environment.json",
                        "deploy/inference-engines/vllm-metax/patches/",
                        "deploy/mooncake/", "third_party/mooncake/",
                    ),
                )
                engine_image = runtime["image"]
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
            dependency_arguments = {
                key: value for key, value in arguments.items() if key in dependency_names
            }
            if component == "model-server" and engine_image:
                dependency_arguments["INFERENCE_ENGINE_IMAGE"] = engine_image
            selected = environments.build(
                component, dockerfile, target="environment",
                arguments=dependency_arguments,
                input_prefixes=("data-plane/patches/vllm/",) if component == "model-server" else (),
            )
            prior = prior_revisions[component]
            manifest = origin.read_manifest(origin.reference(component, prior)) if prior else None
            parts = None
            # Reuse only files from a completed application whose dependency base
            # and compiler inputs still match. Engine binding does not change Rust.
            if (
                component != "control-plane" and manifest is not None
                and manifest.get("image") == previous.get("runtime", {}).get(
                    "model_image" if component == "model-server" else "image"
                )
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
            # Join the file export before publication or releasing its workspace.
            with ThreadPoolExecutor(max_workers=1) as application_build, cancellation:
                payload = publisher.root + "/applications/" + component
                application = application_build.submit(
                    cancellation.run_stage, publisher.export_application, component, payload, arguments, parts,
                )
                if component == "model-server" and engines:
                    cache_prefix = cache_scope["prefix"]
                    engine_arguments = {
                        key: value for key, value in engine_build_arguments(arguments).items()
                        if key != "TARGET_CUDA_ARCH_LIST"
                    }
                    engine_arguments["RUNTIME_IMAGE"] = selected["image"]
                    node_arguments = {}
                    # Dependency exports can acquire new OCI digests on every retry.
                    # The engine base and selected interpreter own Python/accelerator ABI;
                    # CMake and ccache track native source/tool inputs within that environment.
                    python_executable = arguments.get("FORETOKEN_VLLM_PYTHON") or "python3"
                    for builder in builders:
                        base_digest = builder.resolve_image(engine_image)
                        node_arguments[builder.node] = {
                            "RUNTIME_USER": builder.image_user(selected["image"]),
                            "CACHE_ID": f"{cache_prefix}-metadata-{base_digest}-{python_executable}",
                        }
                        key = "" if registry else builder.node
                        engine_caches[key] = f"{cache_prefix}-native-{base_digest}-{python_executable}"
                    pending_state["build"]["engine_caches"] = dict(engine_caches)
                    write_source_state(pending_path, pending_state)
                    selected = environments.build(
                        "engine-source", "deploy/inference-engines/vllm/source-build.Dockerfile",
                        target="environment", arguments=engine_arguments,
                        node_arguments=node_arguments,
                        input_prefixes=(
                            "engine/", "deploy/inference-engines/vllm/source-build.py",
                            "deploy/inference-engines/vllm-metax/source-environment.json",
                            "deploy/inference-engines/vllm-metax/sdk_audio.py",
                            "deploy/inference-engines/vllm-metax/patches/",
                            "data-plane/patches/vllm/",
                        ),
                    )
                application.result()
                if component == "model-server" and engines:
                    export_engine_source(
                        publisher, payload, arguments,
                        runtime_image=selected["image"],
                        cache_id=engine_caches["" if registry else origin.node],
                        build_native=engine_native,
                    )
                return selected

        with ThreadPoolExecutor(max_workers=len(references)) as builds, cancellation:
            pending_builds = {
                component: builds.submit(cancellation.run_stage, build_component, component, dockerfile)
                for component, dockerfile in (
                    ("control-plane", "control-plane/Dockerfile"),
                    ("frontend", "data-plane/frontend/Dockerfile"),
                    ("model-server", "data-plane/model-server/Dockerfile"),
                ) if component in components
            }
            selected_environments = {
                component: future.result() for component, future in pending_builds.items()
            }
        digests: dict[str, dict[str, str]] = {builder.node: {} for builder in builders}
        for component, record in selected_environments.items():
            references[component] = record["image"]
            for node, digest in record["digests"].items():
                digests[node][component] = digest
        # The publisher compares actual exports, independently of download-source choices.
        # Prefer the latest deployment bundle over the initial installation's application.
        retained_applications = None
        if checkout is not None:
            from foretoken.platform.config import default_platform_config
            from foretoken.platform.helm import Helm

            retained_applications = origin.references(
                Helm(default_platform_config(command.oci_registry)).application_history()
            )
            if retained_applications is not None:
                retained_applications.add(suffix)
                retained_applications.update(
                    bundle["revision"]
                    for bundle in [
                        *previous.get("bundles", {}).values(),
                        *build.get("applications", {}).values(),
                    ]
                )
        # Candidate GC is binding-scoped; retain every in-flight component until
        # service intent is committed. Platform publication defers GC to install.
        with ThreadPoolExecutor(max_workers=len(references)) as publications, cancellation:
            pending = {}
            for component, image in references.items():
                prior = prior_revisions[component]
                pending[component] = publications.submit(
                    cancellation.run_stage, origin.publish,
                    publisher,
                    publisher.root + "/applications/" + component,
                    component,
                    suffix,
                    prior,
                    retained_applications,
                    timeout=command.timeout,
                    reuse_previous=True,
                    sources=build_sources(snapshot, component),
                    image=image,
                    parts=application_parts[component],
                    previous_image=previous.get("runtime", {}).get(
                        "model_image" if component == "model-server" else "image"
                    ),
                )
            application_revisions = {
                component: future.result() for component, future in pending.items()
            }
        publisher.run(["rm", "-rf", "--", publisher.root + "/applications"], container=publisher.artifact_container)
        applications = {
            component: origin.reference(component, revision)
            for component, revision in application_revisions.items()
        }
        # Keep builders alive until platform dependencies finish. A failed
        # prerequisite exits through cleanup without delivering candidate images.
        if before_delivery is not None:
            before_delivery()
        for name, record in environments.records.items():
            for node, digest in record["digests"].items():
                digests[node]["environment-" + name] = digest
        environment_images = {
            "environment-" + name: record["image"]
            for name, record in environments.records.items()
        }
        for builder in builders:
            environments.deliver(builder)
            builder.discard_unselected_images(
                set(references.values()) | set(environment_images.values())
            )
        build_contexts.close()
        BuildInputs.select(root, engines, snapshot, mooncake_version=mooncake_version).validate(snapshot)
        yield SourceImages(
            root,
            "registry" if registry else "import",
            references.get("control-plane", ""),
            references["frontend"],
            references["model-server"],
            snapshot,
            {
                "binding": binding,
                "caches": caches,
                "configuration": configuration,
                "versions": versions,
                "digests": digests,
                "images": {**references, **environment_images},
                "environments": environments.records,
                "arguments": arguments,
                "registry_mirrors": registry_mirrors,
                "registry": registry,
                "containerd_socket": nodes[0][1],
                "engine_caches": engine_caches,
                **({"engine_cache_scope": cache_scope} if cache_scope is not None else {}),
                "engine_native": engine_native,
                "runtime_backend": runtime_backend,
                "build_metax_runtime": build_metax_runtime,
                "applications": {
                    component: {"revision": revision}
                    for component, revision in application_revisions.items()
                },
            },
            applications,
        )


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
