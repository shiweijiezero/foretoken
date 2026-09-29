# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare VBench checkpoints in a container and publish project-local settings."""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess

import yaml

from benchmarks.config.video_evaluation import (
    CUSTOM_INPUT_DIMENSIONS,
    VBenchSetupConfig,
    read_evaluator_config,
    vbench_settings,
)
from benchmarks.integrations.vbench.container import (
    VBenchImage,
    inspect_vbench_image,
    vbench_container_command,
)
from benchmarks.runs.evaluation import run_logged_process


def _prepare_checkpoints(image: VBenchImage, cache: Path, log: Path) -> None:
    """Fetch every supported dimension's weights without starting GPU scoring."""
    script = """
import importlib
from pathlib import Path
import sys
from vbench.utils import init_submodules

dimensions = sys.argv[1:]
for dimension in dimensions:
    importlib.import_module('vbench.' + dimension)
modules = init_submodules(dimensions, local=True)
import torch
torch.hub.load(**modules['subject_consistency'])
from vbench.aesthetic_quality import get_aesthetic_model
get_aesthetic_model(modules['aesthetic_quality'][1])
if not Path(modules['dynamic_degree']['model']).is_file():
    raise FileNotFoundError(modules['dynamic_degree']['model'])
print('VBench dependencies and checkpoints are ready.')
"""
    command = vbench_container_command(image, cache)
    command.extend(("python3", "-c", script, *CUSTOM_INPUT_DIMENSIONS))
    result = run_logged_process(command, log, quiet=True)
    if result:
        raise SystemExit(
            f"VBench checkpoint preparation failed (exit {result}); see {log}. "
            "Correct the image or download error and retry setup."
        )


def setup_vbench(config: VBenchSetupConfig) -> None:
    """Pull a VBench image, prepare checkpoints, then publish its pinned YAML."""
    if shutil.which("docker") is None:
        raise ValueError("VBench setup requires Docker on this machine")
    path = config.config_path
    document = read_evaluator_config(path) if path.is_file() else {"evaluators": {}}
    existing = "vbench" in document["evaluators"]
    settings = vbench_settings(path) if existing else {}
    image = config.image or settings.get("image")
    if not image:
        raise ValueError(
            "Specify a VBench image with '--image IMAGE' or set "
            f"evaluators.vbench.image in {path}"
        )
    cache = Path(settings["cache"]) if existing else config.directory / "cache"
    config.directory.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    present = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if present.returncode:
        pull_log = config.directory / "pull.log"
        if run_logged_process(["docker", "pull", image], pull_log, quiet=False):
            raise SystemExit(f"VBench image pull failed; see {pull_log}")
    identity = inspect_vbench_image(image)
    log = config.directory / "setup.log"
    print(
        f"Preparing checkpoints for all {len(CUSTOM_INPUT_DIMENSIONS)} "
        f"video-quality dimensions; log: {log}",
        flush=True,
    )
    _prepare_checkpoints(identity, cache, log)
    document["evaluators"]["vbench"] = {
        "image": identity.reference,
        "cache": str(cache.resolve()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    command = shlex.join(
        ["foretoken", "eval", "--video", "VIDEO_DIR", "--config", str(path)]
    )
    print(f"VBench ready. Evaluator configuration: {path}\nRun: {command}", flush=True)
