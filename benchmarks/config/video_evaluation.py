# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configuration for VBench custom-input video evaluation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys

import yaml

from benchmarks.config.benchmark import BenchmarkOutputConfig, WandbRunConfig


CUSTOM_INPUT_DIMENSIONS = (
    "subject_consistency",
    "background_consistency",
    "aesthetic_quality",
    "imaging_quality",
    "temporal_style",
    "overall_consistency",
    "human_action",
    "temporal_flickering",
    "motion_smoothness",
    "dynamic_degree",
)
CUSTOM_INPUT_VIDEO_SUFFIXES = frozenset({".mp4", ".gif"})


@dataclass(frozen=True)
class VBenchEvaluationConfig:
    """Keep VBench runtime ownership separate from Foretoken's base environment."""

    videos_path: str
    prompt_file: str | None
    dimensions: tuple[str, ...]
    vbench_python: str
    vbench_root: str
    vbench_cache: str | None
    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig

    def validate(self) -> None:
        """Validate local boundaries before starting the GPU evaluator."""
        videos = Path(self.videos_path)
        if not videos.is_dir():
            raise ValueError(f"video directory does not exist: {videos}")
        if not any(
            path.is_file() and path.suffix.lower() in CUSTOM_INPUT_VIDEO_SUFFIXES
            for path in videos.iterdir()
        ):
            raise ValueError(f"video directory contains no MP4 or GIF files: {videos}")
        if self.prompt_file and not Path(self.prompt_file).is_file():
            raise ValueError(f"prompt file does not exist: {self.prompt_file}")
        if not (Path(self.vbench_root) / "evaluate.py").is_file():
            raise ValueError(
                f"VBench checkout does not contain evaluate.py: {self.vbench_root}"
            )
        if self.vbench_cache and not Path(self.vbench_cache).is_dir():
            raise ValueError(f"VBench cache directory does not exist: {self.vbench_cache}")
        if not Path(self.vbench_python).is_file() and shutil.which(
            self.vbench_python
        ) is None:
            raise ValueError(
                f"VBench Python executable not found: {self.vbench_python}"
            )
        if not self.dimensions:
            raise ValueError(
                "--dimension must select at least one custom-input dimension"
            )
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("--dimension cannot contain duplicate dimensions")
        self.outputs.validate()

    def to_dict(self) -> dict:
        """Return a reproducible, secret-free evaluation configuration."""
        return {
            "videos_path": self.videos_path,
            "prompt_file": self.prompt_file,
            "vbench_cache": self.vbench_cache,
            "output": {"destinations": self.outputs.destinations},
        }


def _resolved_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def _python_executable(value: str) -> str:
    path = Path(value).expanduser()
    return str(path.resolve()) if path.is_file() else value


def evaluator_config_path(config_path: str | None = None) -> Path:
    """Locate project settings for setup and evaluation, defaulting to the current directory."""
    if config_path:
        return Path(config_path).expanduser().resolve()
    return next(
        (
            parent / "foretoken-evaluators.yaml"
            for parent in (Path.cwd(), *Path.cwd().parents)
            if (parent / "foretoken-evaluators.yaml").is_file()
        ),
        Path.cwd() / "foretoken-evaluators.yaml",
    )


def read_evaluator_config(path: Path) -> dict:
    """Read evaluator settings without discarding other evaluator configuration."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid evaluator config {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"evaluator config must be a YAML mapping: {path}")
    evaluators = document.get("evaluators")
    if not isinstance(evaluators, dict):
        raise ValueError(f"evaluator config must contain 'evaluators': {path}")
    return document


def vbench_settings(path: Path) -> dict[str, str]:
    """Resolve VBench runtime paths relative to their owning YAML file."""
    evaluators = read_evaluator_config(path)["evaluators"]
    settings = evaluators.get("vbench", {})
    if not isinstance(settings, dict):
        raise ValueError(f"evaluator config must contain 'evaluators.vbench': {path}")
    resolved: dict[str, str] = {}
    for name in ("python", "root", "cache"):
        value = settings.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"evaluators.vbench.{name} must be a path: {path}")
        candidate = Path(value).expanduser()
        if name == "python" and candidate.parent == Path(".") and candidate.name == value:
            resolved[name] = value
        else:
            resolved[name] = str(
                (path.parent / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
    return resolved


@dataclass(frozen=True)
class VBenchSetupConfig:
    """Describe the local configuration and managed installation owned by setup."""

    config_path: Path
    directory: Path


def parse_vbench_setup_arguments(argv: Sequence[str]) -> VBenchSetupConfig:
    """Parse explicit evaluator preparation without running a video benchmark."""
    parser = argparse.ArgumentParser(
        prog="foretoken eval setup",
        allow_abbrev=False,
        description="Prepare an independent VBench environment and evaluator YAML.",
    )
    parser.add_argument("evaluator", choices=("vbench",))
    parser.add_argument(
        "--config", help="output YAML (default: nearest foretoken-evaluators.yaml)",
    )
    parser.add_argument(
        "--directory", type=_resolved_path,
        help="managed installation directory (default: .foretoken/evaluators/vbench beside the YAML)",
    )
    options = parser.parse_args(argv)
    path = evaluator_config_path(options.config)
    directory = (
        Path(options.directory) if options.directory
        else path.parent / ".foretoken/evaluators/vbench"
    )
    return VBenchSetupConfig(path, directory)


def parse_vbench_evaluation_arguments(argv: Sequence[str]) -> VBenchEvaluationConfig:
    """Parse Foretoken-owned VBench custom-input options."""
    output = BenchmarkOutputConfig()
    tracking = WandbRunConfig()
    parser = argparse.ArgumentParser(
        prog="foretoken eval video",
        allow_abbrev=False,
        description="Score generated videos with VBench custom_input dimensions.",
    )
    parser.add_argument(
        "videos_path",
        type=_resolved_path,
        help=(
            "directory containing MP4 or GIF videos "
            "(a Foretoken video result directory is accepted)"
        ),
    )
    parser.add_argument(
        "--evaluator",
        choices=("vbench",),
        default="vbench",
        help="video evaluator (default: vbench)",
    )
    parser.add_argument(
        "--config",
        type=_resolved_path,
        help="evaluator YAML file (default: nearest foretoken-evaluators.yaml)",
    )
    parser.add_argument(
        "--prompt-file",
        type=_resolved_path,
        help="VBench JSON prompt map; inferred from raw_results.json when available",
    )
    parser.add_argument(
        "--dimension",
        nargs="+",
        choices=CUSTOM_INPUT_DIMENSIONS,
        default=CUSTOM_INPUT_DIMENSIONS,
        help="custom_input dimensions (default: all 10 supported dimensions)",
    )
    parser.add_argument(
        "--vbench-python",
        type=_python_executable,
        help="override evaluators.vbench.python",
    )
    parser.add_argument(
        "--vbench-root",
        type=_resolved_path,
        help="override evaluators.vbench.root (contains evaluate.py)",
    )
    parser.add_argument(
        "--vbench-cache",
        type=_resolved_path,
        help="override evaluators.vbench.cache (VBENCH_CACHE_DIR)",
    )
    parser.add_argument(
        "--output",
        default=output.destinations,
        type=lambda value: tuple(value.split(",")),
        help="local,wandb,quiet (default: local,wandb)",
    )
    parser.add_argument(
        "--output-dir",
        default=output.output_dir,
        help="parent directory for run artifacts (default: results)",
    )
    parser.add_argument("--wandb-project", default=tracking.project)
    parser.add_argument("--wandb-entity", default=tracking.entity)
    parser.add_argument("--wandb-run-name", default=tracking.run_name)
    parser.add_argument("--wandb-group", default=tracking.group)
    options = parser.parse_args(argv)
    path = evaluator_config_path(options.config)
    if options.config and not path.is_file():
        raise ValueError(f"evaluator config file does not exist: {path}")
    settings = vbench_settings(path) if path.is_file() else {}
    vbench_root = options.vbench_root or settings.get("root")
    if not vbench_root:
        raise ValueError(
            "Run 'foretoken eval setup vbench' first, or set evaluators.vbench.root "
            "in foretoken-evaluators.yaml or pass --vbench-root"
        )
    config = VBenchEvaluationConfig(
        videos_path=options.videos_path,
        prompt_file=options.prompt_file,
        dimensions=tuple(options.dimension),
        vbench_python=options.vbench_python or settings.get("python") or sys.executable,
        vbench_root=vbench_root,
        vbench_cache=options.vbench_cache or settings.get("cache"),
        outputs=BenchmarkOutputConfig(options.output, options.output_dir),
        wandb=WandbRunConfig(
            project=options.wandb_project,
            entity=options.wandb_entity,
            run_name=options.wandb_run_name,
            group=options.wandb_group,
        ),
    )
    config.validate()
    return config
