# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Load the installed CLI's trusted node client independently of editable workload sources."""

from __future__ import annotations

from importlib import resources

CLIENT_BINARY = "/opt/foretoken-build-client/foretoken-build-client"


def client_module() -> dict[str, str]:
    """Return packaged Go sources and dependency records for the trusted client init container."""
    directory = resources.files("foretoken.build.cluster").joinpath("native")
    return {
        entry.name: entry.read_text()
        for entry in directory.iterdir()
        if entry.is_file() and (entry.name.endswith(".go") or entry.name in {"go.mod", "go.sum"})
    }


def client_compiler_image(
    module: dict[str, str], arguments: dict[str, str], registry_mirrors: dict[str, list[str]],
) -> str:
    """Select the official Go version required by the packaged client, honoring the build's registry transport."""
    version = next(line.split()[1] for line in module["go.mod"].splitlines() if line.startswith("go "))
    registry = arguments.get("GO_IMAGE_REGISTRY", registry_mirrors.get("docker.io", ["docker.io"])[0]).rstrip("/")
    return f"{registry}/library/golang:{version}-bookworm"
