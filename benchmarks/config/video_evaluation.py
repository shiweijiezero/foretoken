# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configuration for VBench custom-input video evaluation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import shutil

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
    """Select an immutable VBench image and host-owned evaluation artifacts."""

    videos_path: str
    prompt_file: str | None
    dimensions: tuple[str, ...]
    image: str
    cache: str
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
        if not self.image:
            raise ValueError("evaluators.vbench.image must name a Docker image")
        if not Path(self.cache).is_dir():
            raise ValueError(f"VBench cache directory does not exist: {self.cache}; run 'foretoken eval setup vbench'")
        if shutil.which("docker") is None:
            raise ValueError("VBench evaluation requires Docker on this machine")
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
            "vbench_image": self.image,
            "vbench_cache": self.cache,
            "output": {"destinations": self.outputs.destinations},
        }


def _resolved_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


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
    """Read the container image and resolve its host cache beside the YAML."""
    evaluators = read_evaluator_config(path)["evaluators"]
    settings = evaluators.get("vbench", {})
    if not isinstance(settings, dict):
        raise ValueError(f"evaluator config must contain 'evaluators.vbench': {path}")
    if "python" in settings or "root" in settings:
        raise ValueError(
            f"VBench Python/root settings are no longer supported: {path}; "
            "set evaluators.vbench.image to a VBench container image"
        )
    image = settings.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ValueError(f"evaluators.vbench.image must be a Docker image: {path}")
    cache = settings.get("cache", ".foretoken/evaluators/vbench/cache")
    if not isinstance(cache, str) or not cache.strip():
        raise ValueError(f"evaluators.vbench.cache must be a directory: {path}")
    candidate = Path(cache).expanduser()
    return {
        "image": image.strip(),
        "cache": str((path.parent / candidate).resolve()),
    }


@dataclass(frozen=True)
class VBenchSetupConfig:
    """Describe the project configuration and checkpoint cache owned by setup."""

    config_path: Path
    directory: Path
    image: str | None


def parse_vbench_setup_arguments(argv: Sequence[str]) -> VBenchSetupConfig:
    """Parse explicit evaluator preparation without running a video benchmark."""
    parser = argparse.ArgumentParser(
        prog="foretoken eval setup",
        allow_abbrev=False,
        description="Pull a VBench image, prepare checkpoints, and write evaluator YAML.",
    )
    parser.add_argument("evaluator", choices=("vbench",))
    parser.add_argument(
        "--config", help="output YAML (default: nearest foretoken-evaluators.yaml)",
    )
    parser.add_argument(
        "--directory", type=_resolved_path,
        help="checkpoint directory (default: .foretoken/evaluators/vbench beside the YAML)",
    )
    parser.add_argument(
        "--image",
        help="VBench Docker image; required when no VBench YAML exists",
    )
    options = parser.parse_args(argv)
    path = evaluator_config_path(options.config)
    directory = (
        Path(options.directory) if options.directory
        else path.parent / ".foretoken/evaluators/vbench"
    )
    return VBenchSetupConfig(path, directory, options.image)


def add_vbench_evaluation_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach VBench-only options to the shared evaluation parser."""
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
        help="video quality dimensions (default: all 10 supported dimensions)",
    )


def vbench_evaluation_config(options: argparse.Namespace) -> VBenchEvaluationConfig:
    """Resolve shared CLI options and the configured VBench image into one run."""
    path = evaluator_config_path(options.config)
    if options.config and not path.is_file():
        raise ValueError(f"evaluator config file does not exist: {path}")
    if not path.is_file():
        raise ValueError(
            "Run 'foretoken eval setup vbench --image IMAGE' first, or create "
            f"a VBench evaluator YAML at {path}"
        )
    settings = vbench_settings(path)
    config = VBenchEvaluationConfig(
        videos_path=_resolved_path(options.video),
        prompt_file=options.prompt_file,
        dimensions=tuple(options.dimension),
        image=settings["image"],
        cache=settings["cache"],
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
