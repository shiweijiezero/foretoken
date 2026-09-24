# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish video-generation benchmark content to an open Weights & Biases run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import wandb

from benchmarks.config.video import VideoBenchmarkConfig

if TYPE_CHECKING:
    from benchmarks.results.output import BenchmarkRun


_SAMPLE_FIELDS = (
    "index",
    "sample_id",
    "task",
    "prompt",
    "width",
    "height",
    "num_frames",
    "fps",
    "num_inference_steps",
    "seed",
    "success",
    "status_code",
    "e2e_s",
    "queue_wait_s",
    "server_generation_s",
    "preprocess_s",
    "encode_s",
    "media_encode_s",
    "denoise_s",
    "decode_s",
    "postprocess_s",
    "denoise_per_step_s",
    "peak_gpu_memory_mb",
    "reference_tokens",
    "output_bytes",
    "output_path",
    "error",
)


def video_metrics_to_wandb(metrics: dict[str, Any]) -> dict[str, float | int]:
    """Flatten video summary metrics into stable W&B keys."""
    message: dict[str, float | int] = {}
    for key in (
        "request_num",
        "success_num",
        "success_rate",
        "e2e_s",
        "queue_wait_s",
        "server_generation_s",
        "preprocess_s",
        "encode_s",
        "media_encode_s",
        "denoise_s",
        "decode_s",
        "postprocess_s",
        "denoise_per_step_s",
        "reference_tokens",
        "output_bytes",
    ):
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            message[f"video/{key}"] = value
    peak_mb = metrics.get("peak_gpu_memory_mb")
    if isinstance(peak_mb, (int, float)):
        message["video/server/peak_gpu_memory_gib"] = float(peak_mb) / 1024
    return message


def publish_video_wandb(
    config: VideoBenchmarkConfig,
    sdk_run: Any,
    run: BenchmarkRun,
) -> None:
    """Publish video summaries, request rows, media, and artifacts to an open run."""
    raw_results = json.loads(
        run.artifacts["raw_results"].read_text(encoding="utf-8")
    )
    sdk_run.log(video_metrics_to_wandb(run.metrics))

    rows = [
        [result.get(field_name) for field_name in _SAMPLE_FIELDS]
        for result in raw_results
    ]
    sdk_run.log(
        {
            "video/requests": wandb.Table(
                columns=list(_SAMPLE_FIELDS),
                data=rows,
            )
        }
    )

    videos = [
        wandb.Video(
            result["output_path"],
            fps=result["fps"],
            format="mp4",
            caption=(
                f"{result['sample_id']}: {result['task']} "
                f"{result['width']}x{result['height']} "
                f"seed={result['seed']}"
            ),
        )
        for result in raw_results
        if result.get("success")
        and result.get("output_path")
        and Path(result["output_path"]).is_file()
    ]
    if videos:
        sdk_run.log({"video/generated_videos": videos})

    artifact = wandb.Artifact(
        name=f"{config.name}-{sdk_run.id}",
        type="video-generation-benchmark",
    )
    for name in ("config", "raw_results", "metrics"):
        path = run.artifacts.get(name)
        if path is not None and path.is_file():
            artifact.add_file(str(path), name=path.name)
    sdk_run.log_artifact(artifact)
