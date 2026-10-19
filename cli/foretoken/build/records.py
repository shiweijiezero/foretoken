# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Exchange completed development builds without workstation paths or download credentials."""

from __future__ import annotations

import json
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foretoken.errors import DeploymentError

if TYPE_CHECKING:
    from foretoken.applications.files import ApplicationFiles

SOURCE_BUILD_KEY = "source-build.json"
WORKSPACE_SELECTION_KEY = "workspace-selection.json"
WORKSPACE_LABEL = "foretoken.io/source-workspace"
# Download locations do not change the compatibility of already completed images.
# A workstation resolves its own transport settings when it needs another build.
BUILD_TRANSPORT_ARGUMENTS = frozenset({
    "GOPROXY", "GOSUMDB", "FORETOKEN_GITHUB_MIRROR", "FORETOKEN_CARGO_REGISTRY",
    "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "CARGO_NET_GIT_FETCH_WITH_CLI",
})


def completed_build(build: dict[str, Any]) -> dict[str, Any]:
    """Select portable completed outputs for publication, excluding local paths and mutable cache identities."""
    return {
        "build": {key: build[key] for key in (
            "versions", "images", "environments", "compilers", "digests", "registry",
            "configuration", "containerd_socket", "runtime_backend", "build_metax_runtime",
            "engine_native", "applications",
        )},
        "arguments": {key: value for key, value in build["arguments"].items() if key not in BUILD_TRANSPORT_ARGUMENTS},
    }


def retain_workspace(
    origin: ApplicationFiles, binding: str, applications: Iterable[str], images: Iterable[str],
) -> None:
    """Retain a workspace's completed and pending selections across clients until platform uninstall."""
    name = "foretoken-source-" + binding
    selection = json.dumps({"applications": sorted(set(applications)), "images": sorted(set(images))})
    existing = origin.kubectl.get_if_exists("configmap", name, origin.namespace)
    owner = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "name": origin.claim, "uid": origin.claim_uid}
    if existing is not None:
        metadata = existing["metadata"]
        if (
            metadata.get("deletionTimestamp") or metadata.get("labels", {}).get(WORKSPACE_LABEL) != binding
            or not any(
                all(reference.get(key) == owner[key] for key in ("kind", "name", "uid"))
                for reference in metadata.get("ownerReferences", [])
            )
        ):
            raise DeploymentError(f"development workspace {origin.namespace}/{name} is not owned by this platform")
        if existing.get("data", {}).get(WORKSPACE_SELECTION_KEY) == selection:
            return
        existing.setdefault("data", {})[WORKSPACE_SELECTION_KEY] = selection
        origin.kubectl.run(["replace", "-f", "-"], input_text=json.dumps(existing))
    else:
        origin.kubectl.run(["create", "-f", "-"], input_text=json.dumps({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {
                "name": name, "namespace": origin.namespace,
                "labels": {WORKSPACE_LABEL: binding}, "ownerReferences": [owner],
            },
            "data": {WORKSPACE_SELECTION_KEY: selection},
        }))


def restore_build(
    origin: ApplicationFiles, selection: dict[str, Any], directory: Path, timeout: str,
) -> tuple[Path, dict[str, Any]]:
    """Restore one exact completed input inventory from the platform's selected development build."""
    snapshot = directory / ("inputs-" + uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix=".development-", dir=directory) as temporary:
        staging = Path(temporary)
        descriptor = origin.download_source_archive(selection["url"], staging, timeout=timeout)
        record = json.loads(descriptor.read_text())
        archive = origin.download_source_archive(record["archive"], staging, timeout=timeout)
        extracted = staging / "inputs"
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(extracted, filter="data")
        extracted.rename(snapshot)
    return snapshot, record
