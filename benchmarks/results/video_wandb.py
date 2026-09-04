# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Optional Weights & Biases publishing for video-generation benchmarks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoSampleResult


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
        message["video/peak_gpu_memory_gib"] = float(peak_mb) / 1024
    return message


def publish_video_to_wandb(
    config: VideoBenchmarkConfig,
    metrics: dict[str, Any],
    results: list[VideoSampleResult],
    run_dir: Path,
) -> None:
    """Upload completed video request metrics and generated videos."""
    if not config.outputs.includes("wandb"):
        return

    import wandb

    os.environ.setdefault("WANDB_SILENT", "true")
    init_kwargs: dict[str, Any] = {
        "project": config.wandb.project,
        "name": config.wandb.run_name or run_dir.name,
        "config": config.to_dict(),
        "dir": str(run_dir),
        "settings": wandb.Settings(x_stats_sampling_interval=1.0),
    }
    if config.wandb.entity:
        init_kwargs["entity"] = config.wandb.entity
    run = wandb.init(**init_kwargs)
    try:
        summary = video_metrics_to_wandb(metrics)
        run.summary.update(summary)
        run.log(summary)

        rows = [
            [getattr(result, field_name) for field_name in _SAMPLE_FIELDS]
            for result in results
        ]
        run.log(
            {
                "video/requests": wandb.Table(
                    columns=list(_SAMPLE_FIELDS), data=rows
                )
            }
        )

        videos = [
            wandb.Video(
                result.output_path,
                fps=result.fps,
                format="mp4",
                caption=(
                    f"{result.sample_id}: {result.task} "
                    f"{result.width}x{result.height} seed={result.seed}"
                ),
            )
            for result in results
            if result.success
            and result.output_path
            and Path(result.output_path).is_file()
        ]
        if videos:
            run.log({"video/generated_videos": videos})

        artifact = wandb.Artifact(
            name=f"{config.name}-{run.id}", type="video-generation-benchmark"
        )
        for filename in ("config.json", "raw_results.json", "metrics.json"):
            path = run_dir / filename
            if path.is_file():
                artifact.add_file(str(path), name=filename)
        run.log_artifact(artifact)
    finally:
        run.finish()
