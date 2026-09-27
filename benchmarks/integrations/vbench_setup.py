# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare an isolated VBench runtime and publish project-local evaluator settings."""

from __future__ import annotations

import os
from pathlib import Path
import platform
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
from benchmarks.runs.native import run_logged_process


VBENCH_REVISION = "fd18b3d055cb0fc6f066ca90fe2c3c8cbb698490"


def _run_setup_command(command: list[str], log: Path) -> None:
    """Run one preparation stage with retained output and actionable failure reporting."""
    result = run_logged_process(command, log, quiet=False)
    if result:
        raise SystemExit(
            f"VBench setup failed (exit {result}); see {log}. "
            "Correct the error and retry, or install manually and configure YAML."
        )


def _install_runtime(directory: Path) -> dict[str, str]:
    """Install the verified CUDA 12.1 stack only in setup's managed Conda prefix."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError(
            "Automatic VBench setup requires Linux x86_64 with NVIDIA CUDA; "
            "install manually and configure YAML on other platforms"
        )
    conda = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not conda:
        raise ValueError(
            "VBench setup requires Conda; activate Conda or "
            "install VBench manually and configure YAML"
        )
    for tool in ("git", "wget", "unzip"):
        if shutil.which(tool) is None:
            raise ValueError(f"VBench setup requires {tool}; install it before retrying")
    root = directory / "VBench"
    prefix = directory / "env"
    python = prefix / "bin/python"
    directory.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        _run_setup_command(
            ["git", "clone", "--no-checkout", "https://github.com/Vchitect/VBench.git", str(root)],
            directory / "clone.log",
        )
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if not (root / "evaluate.py").is_file():
        _run_setup_command(
            ["git", "-C", str(root), "checkout", "--detach", VBENCH_REVISION],
            directory / "checkout.log",
        )
    elif revision.returncode or revision.stdout.strip() != VBENCH_REVISION:
        raise ValueError(
            f"Managed VBench checkout is not at the supported revision: {root}; "
            "use YAML to select a manual installation instead"
        )
    if not (root / "evaluate.py").is_file():
        raise ValueError(f"Managed VBench checkout is incomplete: {root}")
    if not python.is_file():
        _run_setup_command(
            [conda, "create", "--yes", "--prefix", str(prefix),
             "--override-channels", "--channel", "conda-forge", "python=3.10", "pip"],
            directory / "environment.log",
        )
    # Keep the upstream dependencies out of Foretoken and prevent pyiqa from
    # upgrading the already verified PyTorch/torchvision pair.
    torch = ("torch==2.5.1+cu121", "torchvision==0.20.1+cu121")
    _run_setup_command(
        [str(python), "-m", "pip", "install", *torch,
         "--index-url", "https://download.pytorch.org/whl/cu121"],
        directory / "torch.log",
    )
    _run_setup_command(
        [str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt"),
         *torch, "pyiqa==0.1.10", "setuptools<81"],
        directory / "dependencies.log",
    )
    return {"python": str(python), "root": str(root), "cache": str(directory / "cache")}


def setup_vbench(config: VBenchSetupConfig) -> None:
    """Prepare managed or YAML-configured VBench; publish new paths only after success."""
    path = config.config_path
    document = read_evaluator_config(path) if path.is_file() else {"evaluators": {}}
    has_vbench = "vbench" in document["evaluators"]
    if has_vbench:
        settings = vbench_settings(path)
        if not settings.get("python") or not settings.get("root"):
            raise ValueError(
                f"Existing evaluators.vbench requires python and root: {path}; "
                "correct the YAML before retrying"
            )
        print(
            f"Checking existing VBench installation from {path}; "
            "dependencies will not be changed", flush=True,
        )
    else:
        settings = _install_runtime(config.directory)
    root = Path(settings["root"])
    if not (root / "evaluate.py").is_file():
        raise ValueError(f"VBench checkout does not contain evaluate.py: {root}")
    if not Path(settings["python"]).is_file() and not shutil.which(settings["python"]):
        raise ValueError(f"VBench Python executable not found: {settings['python']}")
    cache = Path(settings.get("cache", str(Path.home() / ".cache/vbench")))
    cache.mkdir(parents=True, exist_ok=True)
    config.directory.mkdir(parents=True, exist_ok=True)
    log = config.directory / "setup.log"
    # Upstream initialization fetches its checkpoints without GPU evaluation.
    # The aesthetic predictor is fetched by its own upstream loader, and RAFT
    # needs an explicit check because upstream suppresses download failures.
    script = """
import importlib
from pathlib import Path
import sys
from vbench.utils import init_submodules

dimensions = sys.argv[1:]
for dimension in dimensions:
    importlib.import_module('vbench.' + dimension)
modules = init_submodules(dimensions, local=True)
from vbench.aesthetic_quality import get_aesthetic_model
get_aesthetic_model(modules['aesthetic_quality'][1])
if not Path(modules['dynamic_degree']['model']).is_file():
    raise FileNotFoundError(modules['dynamic_degree']['model'])
print('VBench custom_input dependencies and checkpoints are ready.')
"""
    print(
        f"Preparing checkpoints for all {len(CUSTOM_INPUT_DIMENSIONS)} "
        f"custom_input dimensions; log: {log}", flush=True,
    )
    result = run_logged_process(
        [settings["python"], "-c", script, *CUSTOM_INPUT_DIMENSIONS],
        log, quiet=True, cwd=str(root),
        environment={"VBENCH_CACHE_DIR": str(cache), "PYTHONUNBUFFERED": "1"},
    )
    if result:
        raise SystemExit(
            f"VBench setup failed (exit {result}); see {log}. "
            f"Repair the installation and retry, or configure your own environment in {path}"
        )
    if not has_vbench:
        document["evaluators"]["vbench"] = settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    command = shlex.join([
        "foretoken", "eval", "video", "VIDEO_DIR", "--evaluator", "vbench",
        "--config", str(path),
    ])
    print(f"VBench ready. Evaluator configuration: {path}\nRun: {command}", flush=True)
