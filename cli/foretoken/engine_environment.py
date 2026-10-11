# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve the vLLM build base shared by source installation and image build tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


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
