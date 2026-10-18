# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve inference-engine build inputs and assemble source application exports."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foretoken.build.cluster import ClusterBuilder


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
            "BUILD_JOBS", "TARGET_CUDA_ARCH_LIST",
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
        "deploy/inference-engines/source-build.Dockerfile",
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


if __name__ == "__main__":
    _main()
