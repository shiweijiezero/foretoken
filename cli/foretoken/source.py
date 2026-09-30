# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Source image preparation for Foretoken platform installation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken.manifest import DeploymentError
from foretoken.network_sources import (
    SOURCE_SELECTION_POLICY,
    select_source_build_sources,
)


@dataclass(frozen=True)
class SourceImages:
    """Selected deployment image references and build inputs from one source root."""

    source_root: Path
    image_mode: str
    control_plane: str
    frontend: str
    model_server: str
    inputs: Path


@contextmanager
def prepare_source_images(
    source_path: str,
    registry: str | None,
    oci_registry: str | None,
    namespace: str,
    timeout: str,
    inference_engine_image: str | None = None,
    *,
    installed_images: tuple[str, str, str] | None = None,
    build_metax_runtime: bool = False,
) -> Iterator[SourceImages]:
    """Own image preparation inputs until platform installation commits or fails."""
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
            "FORETOKEN_BUILD_METAX_RUNTIME",
            "DEV_TIMEOUT",
            "IMAGE_PULL_SECRET",
            "INFERENCE_ENGINE_IMAGE",
            "FORETOKEN_VLLM_PYTHON",
            "LOCAL_IMAGE_PREFIX",
            "K3D_CONFIG",
            "DEPLOY_TAG",
            "DEV_IMAGE_OUTPUT",
            "FORETOKEN_INSTALLED_CONTROL_PLANE_IMAGE",
            "FORETOKEN_INSTALLED_FRONTEND_IMAGE",
            "FORETOKEN_INSTALLED_MODEL_SERVER_IMAGE",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "DEV_IMAGE_OUTPUT": str(output_path),
                "FORETOKEN_CLI_SOURCE": "true",
                "REGISTRY": normalized_registry,
                "FORETOKEN_OCI_REGISTRY": oci_registry or "",
                "PLATFORM_NAMESPACE": namespace,
                "DEV_TIMEOUT": timeout,
            }
        )
        if installed_images is not None:
            environment.update(
                {
                    "FORETOKEN_INSTALLED_CONTROL_PLANE_IMAGE": installed_images[0],
                    "FORETOKEN_INSTALLED_FRONTEND_IMAGE": installed_images[1],
                    "FORETOKEN_INSTALLED_MODEL_SERVER_IMAGE": installed_images[2],
                }
            )
        selected_sources, selections, unavailable_sources = (
            select_source_build_sources(environment)
        )
        environment.update(selected_sources)
        for selection in selections:
            print(f"Source mirror selected: {selection}", flush=True)
        if inference_engine_image is not None:
            environment["INFERENCE_ENGINE_IMAGE"] = inference_engine_image
        if build_metax_runtime:
            environment["FORETOKEN_BUILD_METAX_RUNTIME"] = "true"
        from foretoken.editable import capture_build_inputs, validate_build_inputs

        prepared = subprocess.run(["make", "vllm-source"], cwd=source_root, env=environment, check=False)
        if prepared.returncode:
            raise DeploymentError("could not prepare the pinned vLLM build source")
        with capture_build_inputs(source_root) as inputs:
            completed = subprocess.run(
                [str(script)],
                cwd=source_root,
                env=environment,
                check=False,
            )
            if completed.returncode:
                unavailable = (
                    "; unavailable within the "
                    f"{SOURCE_SELECTION_POLICY.probe_timeout_seconds:g}s source probe budget: "
                    + ", ".join(unavailable_sources)
                    if unavailable_sources
                    else ""
                )
                raise DeploymentError(
                    "source image preparation failed with exit code "
                    f"{completed.returncode}{unavailable}"
                )
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeploymentError("source image preparation returned invalid JSON") from exc
            validate_build_inputs(source_root, inputs)
            yield _decode_source_images(source_root, value, inputs)


def _decode_source_images(source_root: Path, value: Any, inputs: Path) -> SourceImages:
    """Decode the JSON contract produced by deploy/dev-deploy."""
    if not isinstance(value, dict):
        raise DeploymentError("source image preparation returned an invalid object")
    string_fields = (
        "IMAGE_MODE",
        "CONTROL_PLANE_DEPLOY_IMAGE",
        "FRONTEND_DEPLOY_IMAGE",
        "MODEL_SERVER_DEPLOY_IMAGE",
    )
    if not all(isinstance(value.get(field), str) for field in string_fields):
        raise DeploymentError("source image preparation returned invalid fields")
    if value["IMAGE_MODE"] not in {"import", "registry"}:
        raise DeploymentError("source image preparation returned an invalid image mode")
    return SourceImages(
        source_root,
        value["IMAGE_MODE"],
        value["CONTROL_PLANE_DEPLOY_IMAGE"],
        value["FRONTEND_DEPLOY_IMAGE"],
        value["MODEL_SERVER_DEPLOY_IMAGE"],
        inputs,
    )
