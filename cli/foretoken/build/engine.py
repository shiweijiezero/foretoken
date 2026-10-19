# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve inference-engine build inputs and assemble source application exports."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foretoken.build.cluster.builder import ClusterBuilder
    from foretoken.build.environments import BuildEnvironments
    from foretoken.command_execution import CommandExecution


def _main() -> None:
    """Print the resolved base for repository build scripts without importing the CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", default="")
    parser.add_argument("--docker-registry", default="")
    arguments = parser.parse_args()
    print(
        resolve_vllm_base_image(
            arguments.root, arguments.image, arguments.docker_registry
        )
    )


def resolve_vllm_base_image(
    root: Path, image: str | None = None, docker_registry: str = ""
) -> str:
    """Read the checkout's build base, preserving explicit images and qualified registries."""
    if image:
        return image
    configuration = root / "deploy/inference-engines/vllm/source-environment.json"
    image = json.loads(configuration.read_text())["baseImage"]
    registry, separator, _ = image.partition("/")
    if separator and ("." in registry or ":" in registry or registry == "localhost"):
        return image
    return f"{docker_registry.rstrip('/') or 'docker.io'}/{image}"


def prepare_mooncake_source(
    root: Path,
    previous: Path | None,
    version: str,
    environment: dict[str, str],
    cancellation: CommandExecution,
) -> None:
    """Prepare the MetaX runtime's fixed dependency before source inputs are captured."""
    from foretoken.build.inputs import mooncake_inputs
    from foretoken.errors import DeploymentError

    _, dependency = mooncake_inputs(root, previous, version)
    if not dependency["prepared"]:
        prepared = cancellation.run(
            [str(root / "deploy/mooncake/prepare-source")],
            cwd=root,
            env=environment,
        )
        if prepared.returncode:
            raise DeploymentError("could not prepare the MetaX runtime's Mooncake source")


def build_metax_environment(
    environments: BuildEnvironments, arguments: dict[str, str],
) -> str:
    """Build the selected MetaX SDK/runtime through the environment owner and return its vLLM image."""
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
    return runtime["image"]


def build_engine_environment(
    environments: BuildEnvironments,
    arguments: dict[str, str],
    *,
    runtime_image: str,
    base_image: str,
    cache_prefix: str,
    record_caches: Callable[[dict[str, str]], None],
) -> dict[str, Any]:
    """Return the vLLM source environment after recording ABI-scoped compiler identities before its solve."""
    engine_arguments = {
        key: value for key, value in engine_build_arguments(arguments).items()
        if key != "TARGET_CUDA_ARCH_LIST"
    }
    engine_arguments["RUNTIME_IMAGE"] = runtime_image
    node_arguments = {}
    engine_caches = {}
    # A rebuilt dependency image can have a new OCI identity without changing
    # the engine ABI. Native workspaces follow the base and interpreter instead.
    python_executable = arguments.get("FORETOKEN_VLLM_PYTHON") or "python3"
    for builder in environments.builders:
        base_digest = builder.resolve_image(base_image)
        node_arguments[builder.node] = {
            "RUNTIME_USER": builder.image_user(runtime_image),
            "CACHE_ID": f"{cache_prefix}-metadata-{base_digest}-{python_executable}",
        }
        key = "" if environments.push else builder.node
        engine_caches[key] = f"{cache_prefix}-native-{base_digest}-{python_executable}"
    record_caches(engine_caches)
    return environments.build(
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


def engine_cache_scope(
    binding: str,
    engines: Iterable[str],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep a binding's compiler namespace across recipe changes and failed builds.

    Callers separate metadata and native workspaces by engine base and interpreter,
    not by the digest of a newly built dependency environment.
    """
    names = sorted(engines)
    if previous and previous["binding"] == binding and previous["engines"] == names:
        return previous
    return {
        "binding": binding,
        "engines": names,
        "prefix": "-".join((binding, uuid.uuid4().hex, *names)),
    }


def engine_build_arguments(arguments: dict[str, str]) -> dict[str, str]:
    """Select the inputs consumed by engine source builds, excluding SDK and Rust-only images."""
    return {
        name: arguments[name]
        for name in (
            "UV_IMAGE_REGISTRY", "UV_IMAGE", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
            "FORETOKEN_GITHUB_MIRROR", "BUILDKIT_SYNTAX_IMAGE",
            "TARGET_CUDA_ARCH_LIST",
        )
        if name in arguments
    }


def export_engine_source(
    builder: ClusterBuilder, payload: str, arguments: dict[str, str], *,
    runtime_image: str, cache_id: str, build_native: bool,
) -> None:
    """Build and merge engine sources into a completed model-server export before publication."""
    output = payload + "-engine"
    builder.build(
        "deploy/inference-engines/vllm/source-build.Dockerfile",
        target="source-export", destination=output,
        arguments={
            **engine_build_arguments(arguments),
            "RUNTIME_IMAGE": runtime_image,
            "CACHE_ID": cache_id,
            "BUILD_NATIVE": str(build_native).lower(),
        },
    )
    builder.run(
        ["sh", "-ec", 'cp -R "$1/." "$2/"', "assemble", output, payload],
        container=builder.artifact_container,
    )


if __name__ == "__main__":
    _main()
