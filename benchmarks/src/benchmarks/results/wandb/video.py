# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish video-generation benchmark content to an open Weights & Biases run."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import wandb

from benchmarks.results.recording.database import read_recorded_requests

if TYPE_CHECKING:
    from benchmarks.results.types import BenchmarkRun


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
    "client_e2e_s",
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
        "failed_num",
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
    sdk_run: Any,
    run: BenchmarkRun,
) -> None:
    """Publish video summaries, request rows and media to the final online session."""
    directory = run.artifacts["raw_results"].parent
    raw_results = sorted(read_recorded_requests(directory, "raw_results"), key=lambda row: row["index"])
    videos = []
    for result in raw_results:
        if not result.get("success") or not result.get("output_path"):
            continue
        relative = Path(result["output_path"])
        # Legacy formal videos were saved beside raw_results.json.
        if relative.is_absolute():
            relative = Path(relative.name)
        path = directory / relative
        if not path.is_file():
            raise ValueError(f"Recorded video is missing: {path}")
        result["output_path"] = relative.as_posix()
        videos.append(wandb.Video(
            str(path), fps=result["fps"], format="mp4",
            caption=(
                f"{result['sample_id']}: {result['task']} "
                f"{result['width']}x{result['height']} seed={result['seed']}"
            ),
        ))
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

    if videos:
        sdk_run.log({"video/generated_videos": videos})
