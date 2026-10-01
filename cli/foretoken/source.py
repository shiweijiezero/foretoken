# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Cluster-side image preparation for source-installed Foretoken platforms."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foretoken.arguments import InstallCommand
from foretoken.cluster_build import (
    ClusterBuilder,
    registry_credentials,
    remove_build_pods,
)
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError
from foretoken.network_sources import select_source_build_sources

_BUILD_CACHE_LABEL = "inference.foretoken.io/source-build-cache"


@dataclass(frozen=True)
class SourceImages:
    """Image references and the cluster build state committed after successful installation."""

    source_root: Path
    image_mode: str
    control_plane: str
    frontend: str
    model_server: str
    inputs: Path
    build_state: dict[str, Any]


def pinned_rust_revision(root: Path) -> str:
    """Read the repository's dependency identity without downloading its source to the client."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--stage",
            "--",
            "data-plane/third_party/vllm",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) != 4 or fields[0] != "160000":
        raise DeploymentError(
            "source installation requires the recorded vLLM Git submodule"
        )
    return fields[1]


def build_configuration(
    root: Path, values: Sequence[dict[str, Any]], image_registry: str
) -> dict[str, str]:
    """Resolve compiler storage and image choices from the chart's authoritative defaults."""
    defaults = yaml.safe_load(
        (root / "deploy/charts/foretoken/values.yaml").read_text()
    )
    configuration = dict(defaults["development"]["build"])
    explicit_image = False
    for value in values:
        overrides = (value.get("development") or {}).get("build") or {}
        explicit_image = explicit_image or "image" in overrides
        configuration.update(overrides)
    if image_registry and not explicit_image:
        configuration["image"] = (
            image_registry.rstrip("/") + "/" + configuration["image"].split("/", 1)[1]
        )
    return configuration


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
            "UV_PYTHON",
            "FORETOKEN_VLLM_PYTHON",
            "BUILD_JOBS",
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
        result.update(BASE_IMAGE_REGISTRY=docker, GO_IMAGE_REGISTRY=docker)
    if ghcr:
        result.update(UV_IMAGE_REGISTRY=ghcr, INFERENCE_ENGINE_IMAGE_REGISTRY=ghcr)
    if gcr:
        result["DISTROLESS_IMAGE_REGISTRY"] = gcr
    return result


def ensure_build_cache(
    kubectl: Kubectl,
    namespace: str,
    name: str,
    configuration: dict[str, str],
    *,
    owner: dict[str, str] | None = None,
) -> None:
    """Create or resize the compiler cache without taking over an unrelated volume."""
    if not kubectl.exists("namespace", namespace):
        kubectl.run(["create", "namespace", namespace])
    existing = kubectl.get_if_exists("pvc", name, namespace)
    if existing is not None:
        if existing["metadata"].get("labels", {}).get(_BUILD_CACHE_LABEL) != "true":
            raise DeploymentError(f"{namespace}/{name} is not a Foretoken build cache")
        spec = {"resources": {"requests": {"storage": configuration["storageSize"]}}}
        if configuration["storageClassName"]:
            spec["storageClassName"] = configuration["storageClassName"]
        if any(existing["spec"].get(key) != value for key, value in spec.items()):
            kubectl.run(
                [
                    "patch",
                    "pvc",
                    name,
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    json.dumps({"spec": spec}),
                ]
            )
        return
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": configuration["storageSize"]}},
    }
    if configuration["storageClassName"]:
        spec["storageClassName"] = configuration["storageClassName"]
    kubectl.run(
        ["create", "-f", "-"],
        input_text=yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {_BUILD_CACHE_LABEL: "true"},
                    **({"ownerReferences": [owner]} if owner is not None else {}),
                },
                "spec": spec,
            }
        ),
    )


def remove_build_caches(kubectl: Kubectl, timeout: str) -> None:
    """Release managed build Pods before their compiler volumes, leaving model caches intact."""
    remove_build_pods(kubectl, timeout)
    kubectl.run(
        [
            "delete",
            "pvc",
            "--all-namespaces",
            "-l",
            _BUILD_CACHE_LABEL + "=true",
            "--ignore-not-found",
            "--timeout=" + timeout,
        ]
    )


def local_build_nodes(
    kubectl: Kubectl, registry: str | None, socket: str = ""
) -> list[tuple[str, str, str]]:
    """Use node-local image stores only for standard local kind and k3d contexts."""
    if registry:
        return [("", "", "foretoken-source-build")]
    if not socket:
        context = kubectl.run(["config", "current-context"]).stdout.strip()
        if context.startswith("k3d-"):
            socket = "/run/k3s/containerd/containerd.sock"
        elif context.startswith("kind-"):
            socket = "/run/containerd/containerd.sock"
        else:
            raise DeploymentError(
                "source installation on a remote cluster requires --registry"
            )
    nodes = [
        (
            node["metadata"]["name"],
            socket,
            "foretoken-source-build-" + node["metadata"]["uid"][:8],
        )
        for node in kubectl.list_resources(("nodes",), "")
        if not node.get("spec", {}).get("unschedulable")
    ]
    if not nodes:
        raise DeploymentError("source builds need a schedulable Kubernetes node")
    return nodes


@contextmanager
def prepare_source_images(
    command: InstallCommand,
    namespace: str,
    values: Sequence[dict[str, Any]],
    inference_engine_image: str | None = None,
    *,
    installed_images: tuple[str, str, str] | None = None,
    build_metax_runtime: bool = False,
    runtime_backend: str = "nvidia",
    saved_arguments: dict[str, str] | None = None,
) -> Iterator[SourceImages]:
    """Build directly in the target cluster and retain inputs until installation commits."""
    from foretoken.editable import (
        _context_identity,
        _state_directory,
        _write_json,
        capture_build_inputs,
        engine_source_roots,
        snapshot_versions,
        validate_build_inputs,
    )

    root = Path(command.editable or "").expanduser().resolve()
    if (
        not (root / "Makefile").is_file()
        or not (root / "deploy/charts/foretoken/Chart.yaml").is_file()
    ):
        raise DeploymentError(
            f"--editable must reference a Foretoken source root: {root}"
        )
    kubectl = Kubectl()
    engines = engine_source_roots(command.engine_sources)
    state_directory = _state_directory(kubectl)
    state_path = state_directory / "install.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    build = previous.get("build", {}) if previous.get("root") == str(root) else {}
    pending_path = state_directory / "build.json"
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    if not build and pending.get("root") == str(root):
        build = pending["build"]
    binding = build.get("binding") or uuid.uuid4().hex
    environment = os.environ.copy()
    if command.oci_registry:
        environment["FORETOKEN_OCI_REGISTRY"] = command.oci_registry
    if saved_arguments is None:
        selected, selections, _ = select_source_build_sources(environment)
        environment.update(selected)
        for selection in selections:
            print(f"Source mirror selected: {selection}", flush=True)
        arguments = build_arguments(environment)
    else:
        environment.update(saved_arguments)
        arguments = dict(saved_arguments)
    if build_metax_runtime:
        prepared = subprocess.run(
            [str(root / "deploy/mooncake/prepare-source")],
            cwd=root,
            env=environment,
            check=False,
        )
        if prepared.returncode:
            raise DeploymentError(
                "could not prepare the MetaX runtime's Mooncake source"
            )
    arguments["VLLM_REVISION"] = pinned_rust_revision(root)
    configuration = build_configuration(
        root, values, arguments.get("BASE_IMAGE_REGISTRY", "")
    )
    registry = (command.registry or "").rstrip("/")
    prefix = registry if registry else "docker.io/library/foretoken-dev"
    suffix = uuid.uuid4().hex
    references = {
        component: f"{prefix}/{component}:{suffix}"
        if registry
        else f"{prefix}-{component}:{suffix}"
        for component in ("control-plane", "frontend", "model-server")
    }
    secret_names = tuple(
        secret["name"]
        for value in values
        for secret in value.get("imagePullSecrets", [])
    )
    credentials = registry_credentials(
        [
            configuration["image"],
            prefix,
            inference_engine_image or "",
            "docker.io",
            "ghcr.io",
            *(value for key, value in arguments.items() if key.endswith("REGISTRY")),
        ]
    )
    nodes = local_build_nodes(
        kubectl, command.registry, build.get("containerd_socket", "")
    )
    # Keep a failed first installation's compiler cache addressable for retry and
    # uninstall, without replacing an existing successful installation binding.
    state_directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        pending_path,
        {
            "root": str(root),
            "context": _context_identity(kubectl),
            "build": {"binding": binding, "containerd_socket": nodes[0][1]},
        },
    )
    for _, _, claim in nodes:
        ensure_build_cache(kubectl, namespace, claim, configuration)
    with capture_build_inputs(root, engines) as snapshot, ExitStack() as build_contexts:
        old = state_directory / previous["inputs"] if previous.get("inputs") else None
        versions = snapshot_versions(snapshot, old, build.get("versions", {}))
        files = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        digests: dict[str, dict[str, str]] = {}
        installed = dict(zip(references, installed_images or (), strict=False))
        reusable = {
            component: build.get("registry") == registry
            and installed.get(component) == build.get("images", {}).get(component)
            and component in installed
            for component in references
        }
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
                    node=node,
                    containerd_socket=socket,
                    pull_secrets=secret_names,
                    credentials=credentials,
                )
            )
            builders.append(builder)
            builder.sync(files, versions)
            engine_image = inference_engine_image
            if build_metax_runtime:
                engine_image = (
                    f"{prefix}/vllm-metax:{suffix}"
                    if registry
                    else f"{prefix}-vllm-metax:{suffix}"
                )
                builder.build(
                    "deploy/inference-engines/vllm-metax/Dockerfile",
                    image=engine_image,
                    push=bool(registry),
                    arguments=arguments,
                )
            node_digests = {}
            for component, dockerfile in (
                ("control-plane", "control-plane/Dockerfile"),
                ("frontend", "data-plane/frontend/Dockerfile"),
                ("model-server", "data-plane/model-server/Dockerfile"),
            ):
                component_arguments = dict(arguments)
                if component == "model-server" and engine_image:
                    component_arguments["INFERENCE_ENGINE_IMAGE"] = engine_image
                print(
                    f"Building {component} in {namespace}/{builder.name}",
                    flush=True,
                )
                image = (
                    references[component] + "-base"
                    if component == "model-server" and engines
                    else references[component]
                )
                metadata = builder.build(
                    dockerfile,
                    image=image,
                    push=bool(registry),
                    arguments=component_arguments,
                )
                final_dockerfile, final_target, final_arguments = (
                    dockerfile,
                    "",
                    component_arguments,
                )
                if component == "model-server" and engines:
                    engine_arguments = {
                        **arguments,
                        "RUNTIME_IMAGE": image,
                        "CACHE_ID": binding
                        + "-"
                        + metadata["containerimage.digest"]
                        + "-"
                        + versions["deploy/inference-engines/source-build.Dockerfile"],
                        "BUILD_NATIVE": str(
                            runtime_backend != "metax" or "vllm-metax" in engines
                        ).lower(),
                    }
                    user_output = builder.root + "/runtime-user"
                    builder.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="runtime-user-export",
                        destination=user_output,
                        arguments=engine_arguments,
                    )
                    engine_arguments["RUNTIME_USER"] = builder.run(
                        ["cat", user_output + "/user"], capture=True
                    ).strip()
                    metadata = builder.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="runtime",
                        image=references[component],
                        push=bool(registry),
                        arguments=engine_arguments,
                    )
                    final_dockerfile, final_target, final_arguments = (
                        "deploy/inference-engines/source-build.Dockerfile",
                        "runtime",
                        engine_arguments,
                    )
                node_digests[component] = metadata["containerimage.digest"]
                reusable[component] = reusable[component] and node_digests[
                    component
                ] == build.get("digests", {}).get(node, {}).get(component)
                if reusable[component]:
                    # Restore an unchanged reference even if its registry tag or local
                    # image record was collected; never overwrite it with new content.
                    if socket:
                        builder.reuse_image_reference(
                            references[component], installed[component]
                        )
                    else:
                        builder.build(
                            final_dockerfile,
                            target=final_target,
                            image=installed[component],
                            push=True,
                            arguments=final_arguments,
                        )
            digests[node] = node_digests
        for component in references:
            if reusable[component]:
                references[component] = installed[component]
        for builder in builders:
            builder.discard_unselected_images(
                set(references.values()) | set(installed.values())
            )
        build_contexts.close()
        validate_build_inputs(root, snapshot, engines)
        yield SourceImages(
            root,
            "registry" if registry else "import",
            references["control-plane"],
            references["frontend"],
            references["model-server"],
            snapshot,
            {
                "binding": binding,
                "configuration": configuration,
                "versions": versions,
                "digests": digests,
                "images": references,
                "arguments": arguments,
                "registry": registry,
                "containerd_socket": nodes[0][1],
                "environment": build["environment"]
                if reusable["model-server"]
                else suffix,
                "backend": runtime_backend,
            },
        )
