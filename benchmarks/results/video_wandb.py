# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Optional Weights & Biases publishing for video-generation benchmarks."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.results.output import BenchmarkRun

logger = logging.getLogger(__name__)


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


class VideoWandbError(RuntimeError):
    """Report a failure in the video-specific W&B output lifecycle."""


class VideoWandbSink:
    """Publish video metrics and media through one ResultOutputs-owned W&B run."""

    def __init__(self, config: VideoBenchmarkConfig, run_dir: Path) -> None:
        self.config = config
        self.run_dir = run_dir
        self.run: Any | None = None
        self.publish_failed = False

    def open(self, record: dict[str, Any]) -> None:
        """Start system monitoring before the video requests execute."""
        os.environ.setdefault("WANDB_SILENT", "true")
        run_config = self.config.to_dict()
        run_config["metric_scopes"] = {
            "wandb_system": "benchmark_client",
            "video_response": "remote_service",
        }
        try:
            import wandb

            init_kwargs: dict[str, Any] = {
                "project": self.config.wandb.project,
                "name": self.config.wandb.run_name or self.run_dir.name,
                "config": run_config,
                "dir": str(self.run_dir),
                "settings": wandb.Settings(x_stats_sampling_interval=1.0),
            }
            if self.config.wandb.entity:
                init_kwargs["entity"] = self.config.wandb.entity
            self.run = wandb.init(**init_kwargs)
        except Exception as exc:
            raise VideoWandbError(
                f"W&B initialization failed: {type(exc).__name__}: {exc}"
            ) from exc
        if self.run is None:
            raise VideoWandbError("W&B initialization returned no run")

    def publish(self, benchmark_run: BenchmarkRun) -> None:
        """Publish video summaries, request rows, media, and metadata artifacts."""
        if self.run is None:
            raise RuntimeError("video W&B sink is not open")
        try:
            import wandb

            raw_results = json.loads(
                benchmark_run.artifacts["raw_results"].read_text(
                    encoding="utf-8"
                )
            )
            summary = video_metrics_to_wandb(benchmark_run.metrics)
            self.run.summary.update(
                {
                    **summary,
                    "video/wandb_system_metrics_scope": "benchmark_client",
                    "video/server_metrics_scope": "remote_service",
                }
            )
            self.run.log(summary)

            rows = [
                [result.get(field_name) for field_name in _SAMPLE_FIELDS]
                for result in raw_results
            ]
            self.run.log(
                {
                    "video/requests": wandb.Table(
                        columns=list(_SAMPLE_FIELDS), data=rows
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
                self.run.log({"video/generated_videos": videos})

            artifact = wandb.Artifact(
                name=f"{self.config.name}-{self.run.id}",
                type="video-generation-benchmark",
            )
            for name in ("config", "raw_results", "metrics"):
                path = benchmark_run.artifacts.get(name)
                if path is not None and path.is_file():
                    artifact.add_file(str(path), name=path.name)
            self.run.log_artifact(artifact)
        except Exception as exc:
            self.publish_failed = True
            raise VideoWandbError(
                f"W&B publication failed: {type(exc).__name__}: {exc}"
            ) from exc

    def close(self) -> None:
        """Finish the W&B run after publication or benchmark failure."""
        run = self.run
        self.run = None
        if run is None:
            return
        try:
            run.finish()
        except Exception as exc:
            if self.publish_failed:
                logger.exception("W&B finalization also failed")
                return
            raise VideoWandbError(
                f"W&B finalization failed: {type(exc).__name__}: {exc}"
            ) from exc
