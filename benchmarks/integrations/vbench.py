# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Translate Foretoken video artifacts into VBench custom-input evaluation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from benchmarks.config.video_evaluation import (
    CUSTOM_INPUT_VIDEO_SUFFIXES,
    VBenchEvaluationConfig,
)
from benchmarks.integrations.video_evaluator import (
    VideoEvaluationCommand,
    VideoEvaluationIdentity,
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
    """Use an explicit prompt map or derive one from Foretoken video results."""
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
        return path
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


def _vbench_commit(root: str) -> str | None:
    """Identify the VBench checkout without borrowing a parent repository's commit."""
    if shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "-C", root, "rev-parse", "--show-toplevel", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 2:
        return None
    checkout, commit = lines
    return commit if Path(checkout).resolve() == Path(root).resolve() else None


class VBenchEvaluator:
    """Adapt VBench custom_input to Foretoken's video evaluation lifecycle."""

    name = "vbench"

    def __init__(self, config: VBenchEvaluationConfig) -> None:
        self.config = config

    def describe(self) -> VideoEvaluationIdentity:
        """Record the selected videos, prompt source, and native code revision."""
        return VideoEvaluationIdentity(
            model=source_model(self.config.videos_path),
            evaluation_mode="custom_input",
            metadata={
                "vbench_python": self.config.vbench_python,
                "vbench_root": self.config.vbench_root,
                "vbench_commit": _vbench_commit(self.config.vbench_root),
                "dimensions": list(self.config.dimensions),
                "prompt_source": _prompt_source(self.config),
                "num_videos": len(_videos(self.config.videos_path)),
            },
        )

    def prepare(self, native_directory: Path) -> VideoEvaluationCommand:
        """Materialize any Foretoken prompt map and invoke official evaluate.py."""
        config = self.config
        prompt_file = prepare_prompt_file(config, native_directory)
        arguments = [
            config.vbench_python,
            str(Path(config.vbench_root) / "evaluate.py"),
            "--videos_path",
            config.videos_path,
            "--dimension",
            *config.dimensions,
            "--mode",
            "custom_input",
            "--output_path",
            str(native_directory),
            "--load_ckpt_from_local",
            "True",
        ]
        if prompt_file is not None:
            arguments.extend(("--prompt_file", str(prompt_file)))
        environment = (
            {"VBENCH_CACHE_DIR": config.vbench_cache}
            if config.vbench_cache
            else {}
        )
        return VideoEvaluationCommand(tuple(arguments), config.vbench_root, environment)

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
