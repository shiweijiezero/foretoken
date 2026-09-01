# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Source image preparation for Foretoken platform installation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken_cli.kubernetes import Kubectl
from foretoken_cli.manifest import DeploymentError


@dataclass(frozen=True)
class SourceImages:
    """Image references and changed components prepared from one source root."""

    source_root: Path
    image_mode: str
    control_plane: str
    frontend: str
    model_server: str
    control_plane_changed: bool
    frontend_changed: bool
    model_server_changed: bool


def prepare_source_images(
    source_path: str,
    registry: str | None,
    namespace: str,
    timeout: str,
    dry_run: bool,
) -> SourceImages:
    """Build and distribute source images, or return their dry-run identities."""
    source_root = Path(source_path).expanduser().resolve()
    script = source_root / "deploy" / "dev-deploy"
    chart = source_root / "deploy" / "charts" / "foretoken" / "Chart.yaml"
    if not (source_root / "Makefile").is_file() or not script.is_file() or not chart.is_file():
        raise DeploymentError(
            f"--editable must reference a Foretoken source root: {source_root}"
        )

    normalized_registry = (registry or "").rstrip("/")
    with tempfile.TemporaryDirectory(prefix="foretoken-source-") as directory:
        output_path = Path(directory) / "images.json"
        environment = os.environ.copy()
        for key in (
            "CLUSTER",
            "KIND_CLUSTER",
            "KIND_CONFIG",
            "REGISTRY",
            "RELEASE",
            "PLATFORM_NAMESPACE",
            "WORKLOAD_NAMESPACE",
            "FRONTEND_MODE",
            "FORETOKEN_CLI_SOURCE",
            "DEV_TIMEOUT",
            "IMAGE_PULL_SECRET",
            "INFERENCE_ENGINE_IMAGE",
            "LOCAL_IMAGE_PREFIX",
            "K3D_CONFIG",
            "TAG",
            "DEPLOY_TAG",
            "DEV_IMAGE_OUTPUT",
            "DEV_IMAGE_PLAN_ONLY",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "DEV_IMAGE_OUTPUT": str(output_path),
                "DEV_IMAGE_PLAN_ONLY": "true" if dry_run else "false",
                "FORETOKEN_CLI_SOURCE": "true",
                "REGISTRY": normalized_registry,
                "PLATFORM_NAMESPACE": namespace,
                "DEV_TIMEOUT": timeout,
            }
        )
        completed = subprocess.run(
            [str(script)],
            cwd=source_root,
            env=environment,
            check=False,
        )
        if completed.returncode:
            raise DeploymentError(
                f"source image preparation failed with exit code {completed.returncode}"
            )
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError("source image preparation returned invalid JSON") from exc
    return _decode_source_images(source_root, value)


def restart_changed_source_deployments(
    kubectl: Kubectl,
    images: SourceImages,
    platform_namespace: str,
    timeout: str,
) -> None:
    """Restart imported-image workloads whose local image content changed."""
    if images.image_mode != "import":
        return
    stages = (
        (
            images.control_plane_changed,
            "app.kubernetes.io/name=foretoken-control-plane,"
            "app.kubernetes.io/instance=foretoken",
            platform_namespace,
        ),
        (
            images.frontend_changed,
            "inference.foretoken.io/frontend-service",
            "",
        ),
        (
            images.model_server_changed,
            "inference.foretoken.io/model-group",
            "",
        ),
    )
    for changed, selector, namespace_scope in stages:
        if not changed:
            continue
        for deployment in kubectl.list_all_resources(
            ("deployment.apps",), label_selector=selector
        ):
            metadata = deployment.get("metadata") or {}
            name = str(metadata.get("name") or "")
            namespace = str(metadata.get("namespace") or "")
            if (
                name
                and namespace
                and (not namespace_scope or namespace == namespace_scope)
            ):
                kubectl.restart_deployment(name, namespace, timeout)


def _decode_source_images(source_root: Path, value: Any) -> SourceImages:
    """Decode the JSON contract produced by deploy/dev-deploy."""
    if not isinstance(value, dict):
        raise DeploymentError("source image preparation returned an invalid object")
    string_fields = (
        "IMAGE_MODE",
        "CONTROL_PLANE_DEPLOY_IMAGE",
        "FRONTEND_DEPLOY_IMAGE",
        "MODEL_SERVER_DEPLOY_IMAGE",
    )
    boolean_fields = (
        "CONTROL_PLANE_CHANGED",
        "FRONTEND_CHANGED",
        "MODEL_SERVER_CHANGED",
    )
    if not all(isinstance(value.get(field), str) for field in string_fields) or not all(
        isinstance(value.get(field), bool) for field in boolean_fields
    ):
        raise DeploymentError("source image preparation returned invalid fields")
    if value["IMAGE_MODE"] not in {"import", "registry"}:
        raise DeploymentError("source image preparation returned an invalid image mode")
    return SourceImages(
        source_root,
        value["IMAGE_MODE"],
        value["CONTROL_PLANE_DEPLOY_IMAGE"],
        value["FRONTEND_DEPLOY_IMAGE"],
        value["MODEL_SERVER_DEPLOY_IMAGE"],
        value["CONTROL_PLANE_CHANGED"],
        value["FRONTEND_CHANGED"],
        value["MODEL_SERVER_CHANGED"],
    )
