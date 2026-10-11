# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Translate Foretoken video artifacts into VBench custom-input evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.config.video_evaluation import (
    CUSTOM_INPUT_VIDEO_SUFFIXES,
    VBenchEvaluationConfig,
)
from benchmarks.integrations.video_evaluator import (
    VideoEvaluationCommand,
    VideoEvaluationIdentity,
)
from benchmarks.integrations.vbench.container import (
    VBENCH_OUTPUT,
    VBENCH_ROOT,
    VBENCH_VIDEOS,
    inspect_vbench_image,
    vbench_container_command,
)
from benchmarks.results.output import write_json


def _videos(directory: str) -> list[Path]:
    """Match the top-level video formats consumed by VBench custom_input."""
    return sorted(
        path
        for path in Path(directory).iterdir()
        if path.is_file() and path.suffix.lower() in CUSTOM_INPUT_VIDEO_SUFFIXES
    )


def _prompt_source(config: VBenchEvaluationConfig) -> str:
    """Name the prompt input that will be passed to VBench for this run."""
    if config.prompt_file:
        return "prompt_file"
    if (Path(config.videos_path) / "raw_results.json").is_file():
        return "raw_results.json"
    return "filename"


def prepare_prompt_file(
    config: VBenchEvaluationConfig, native_directory: Path
) -> Path | None:
    """Copy or derive a prompt map inside the host directory mounted as output."""
    if config.prompt_file:
        path = Path(config.prompt_file)
        prompts = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prompts, dict) or not all(
            isinstance(name, str) and isinstance(prompt, str)
            for name, prompt in prompts.items()
        ):
            raise ValueError(
                "--prompt-file must contain a JSON object mapping video paths to prompts"
            )
        normalized: dict[str, str] = {}
        for name, prompt in prompts.items():
            filename = Path(name).name
            if filename in normalized and normalized[filename] != prompt:
                raise ValueError(f"--prompt-file contains conflicting prompts for {filename}")
            normalized[filename] = prompt
        return write_json(str(native_directory), "prompt_map.json", normalized)
    raw_results = Path(config.videos_path) / "raw_results.json"
    if not raw_results.is_file():
        return None
    results = json.loads(raw_results.read_text(encoding="utf-8"))
    if not isinstance(results, list):
        raise ValueError(f"expected a JSON list in {raw_results}")
    prompts: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict) or not item.get("success"):
            continue
        output_path = item.get("output_path")
        prompt = item.get("prompt")
        if isinstance(output_path, str) and isinstance(prompt, str) and prompt:
            prompts[Path(output_path).name] = prompt
    videos = _videos(config.videos_path)
    missing = [video.name for video in videos if video.name not in prompts]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"raw_results.json has no prompt for video(s): {names}")
    path = write_json(str(native_directory), "prompt_map.json", prompts)
    return path


class VBenchEvaluator:
    """Adapt VBench custom_input to Foretoken's video evaluation lifecycle."""

    name = "vbench"

    def __init__(self, config: VBenchEvaluationConfig) -> None:
        self.config = config
        self.image = inspect_vbench_image(config.image)

    def describe(self) -> VideoEvaluationIdentity:
        """Record the selected videos, prompt source, and native code revision."""
        return VideoEvaluationIdentity(
            model=source_model(self.config.videos_path),
            evaluation_mode="custom_input",
            metadata={
                "vbench_image": self.config.image,
                "vbench_image_id": self.image.image_id,
                "vbench_image_digest": self.image.digest,
                "vbench_commit": self.image.commit,
                "dimensions": list(self.config.dimensions),
                "prompt_source": _prompt_source(self.config),
                "num_videos": len(_videos(self.config.videos_path)),
            },
        )

    def prepare(self, native_directory: Path) -> VideoEvaluationCommand:
        """Mount Foretoken inputs and invoke VBench inside the selected image."""
        config = self.config
        prompt_file = prepare_prompt_file(config, native_directory)
        arguments = vbench_container_command(
            self.image,
            Path(config.cache),
            videos=Path(config.videos_path),
            output=native_directory,
            gpu=True,
        )
        arguments.extend((
            "python3",
            f"{VBENCH_ROOT}/evaluate.py",
            "--videos_path",
            VBENCH_VIDEOS,
            "--dimension",
            *config.dimensions,
            "--mode",
            "custom_input",
            "--output_path",
            VBENCH_OUTPUT,
            "--load_ckpt_from_local",
            "True",
        ))
        if prompt_file is not None:
            arguments.extend(("--prompt_file", f"{VBENCH_OUTPUT}/{prompt_file.name}"))
        return VideoEvaluationCommand(tuple(arguments), None, {})

    def read_metrics(self, directory: Path) -> dict[str, Any]:
        """Normalize VBench aggregate dimension scores for shared result sinks."""
        paths = sorted(directory.glob("*_eval_results.json"))
        if not paths:
            return {"scores": [], "execution": {}}
        rows: list[dict[str, Any]] = []
        for path in paths:
            result = json.loads(path.read_text(encoding="utf-8"))
            for dimension, value in result.items():
                aggregate = value[0] if isinstance(value, list) and value else value
                details = value[1] if isinstance(value, list) and len(value) > 1 else None
                rows.append(
                    {
                        "task": "custom_input",
                        "level": "task",
                        "subset": "",
                        "filter": "custom_input",
                        "metric": dimension,
                        "value": aggregate,
                        "stderr": None,
                        "samples": len(details) if isinstance(details, list) else None,
                        "direction": True,
                        "display_multiplier": 1,
                        "display_unit": "",
                        "primary": None,
                    }
                )
        return {"scores": rows, "execution": {}}


def source_model(videos_path: str) -> str:
    """Infer the model name when the source is a Foretoken result directory."""
    raw_results = Path(videos_path) / "raw_results.json"
    if raw_results.is_file():
        results = json.loads(raw_results.read_text(encoding="utf-8"))
        models = {
            item.get("model")
            for item in results
            if isinstance(item, dict)
            and item.get("success")
            and isinstance(item.get("model"), str)
            and item.get("model")
        }
        if len(models) == 1:
            return str(models.pop())
        if len(models) > 1:
            return "multiple-models"
    return Path(videos_path).name
