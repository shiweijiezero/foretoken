# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own VBench image identity and the host-to-container execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess


VBENCH_ROOT = "/opt/VBench"
VBENCH_CACHE = "/cache"
VBENCH_VIDEOS = "/videos"
VBENCH_OUTPUT = "/output"
VBENCH_REVISION_LABEL = "org.foretoken.vbench.commit"


@dataclass(frozen=True)
class VBenchImage:
    """Identify the exact local image and VBench source used by a run."""

    image_id: str
    digest: str | None
    commit: str

    @property
    def reference(self) -> str:
        """Prefer a pullable digest when publishing setup configuration."""
        return self.digest or self.image_id


def inspect_vbench_image(reference: str) -> VBenchImage:
    """Inspect an already-present VBench image without pulling or running it."""
    if shutil.which("docker") is None:
        raise ValueError("VBench requires Docker on this machine")
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            f"VBench image is unavailable: {reference}; run 'foretoken eval setup vbench'"
        )
    image = json.loads(result.stdout)[0]
    labels = (image.get("Config") or {}).get("Labels") or {}
    commit = labels.get(VBENCH_REVISION_LABEL)
    if not isinstance(commit, str) or not commit:
        raise ValueError(
            f"VBench image lacks the {VBENCH_REVISION_LABEL} label: {reference}"
        )
    digests = image.get("RepoDigests") or []
    return VBenchImage(image["Id"], digests[0] if digests else None, commit)


def vbench_container_command(
    image: VBenchImage,
    cache: Path,
    *,
    videos: Path | None = None,
    output: Path | None = None,
    gpu: bool = False,
) -> list[str]:
    """Bind host-owned paths and selected GPUs into a disposable VBench container."""
    command = ["docker", "run", "--rm"]
    if gpu:
        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        devices = f"device={visible_devices}" if visible_devices else "all"
        command.extend(("--gpus", f'"{devices}"' if "," in devices else devices))
    if hasattr(os, "getuid"):
        command.extend(("--user", f"{os.getuid()}:{os.getgid()}"))
    for source, target, readonly in (
        (cache, VBENCH_CACHE, False),
        (videos, VBENCH_VIDEOS, True),
        (output, VBENCH_OUTPUT, False),
    ):
        if source is not None:
            mount = f"type=bind,source={source.resolve()},target={target}"
            command.extend(("--mount", mount + (",readonly" if readonly else "")))
    command.extend(
        (
            "--env", f"VBENCH_CACHE_DIR={VBENCH_CACHE}",
            "--env", f"HOME={VBENCH_CACHE}",
            "--env", f"HF_HOME={VBENCH_CACHE}/huggingface",
            "--workdir", VBENCH_ROOT,
            image.image_id,
        )
    )
    return command
