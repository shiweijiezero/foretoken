# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configuration for synchronous video-generation benchmarks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from benchmarks.config.benchmark import BenchmarkOutputConfig, WandbRunConfig


@dataclass(frozen=True)
class VideoInputFile:
    """Describe one multipart file field owned by a dataset row."""

    field: str
    path: str
    content_type: str

    def to_dict(self) -> dict[str, str]:
        """Return the normalized multipart file configuration."""
        return {
            "field": self.field,
            "path": self.path,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class VideoDatasetDefaults:
    """Store generation settings applied to public benchmark rows."""

    width: int = 1024
    height: int = 576
    num_frames: int = 124
    fps: int = 24
    num_inference_steps: int = 50
    aspect_ratio: str = "16:9"
    flow_shift: float = 12.0
    audio_flow_shift: float = 3.0
    seed: int = 1


@dataclass(frozen=True)
class VideoGenerationRequest:
    """Represent one complete video-generation request loaded from a dataset."""

    sample_id: str
    task: str
    prompt: str
    width: int
    height: int
    num_frames: int
    fps: int
    num_inference_steps: int
    aspect_ratio: str | None = None
    flow_shift: float | None = None
    audio_flow_shift: float | None = None
    seed: int | None = None
    frame_indices: tuple[int, ...] = ()
    files: tuple[VideoInputFile, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized request configuration persisted with a run."""
        return {
            "id": self.sample_id,
            "task": self.task,
            "prompt": self.prompt,
            "generation": {
                "width": self.width,
                "height": self.height,
                "num_frames": self.num_frames,
                "fps": self.fps,
                "num_inference_steps": self.num_inference_steps,
                "aspect_ratio": self.aspect_ratio,
                "flow_shift": self.flow_shift,
                "audio_flow_shift": self.audio_flow_shift,
                "seed": self.seed,
            },
            "frame_indices": list(self.frame_indices),
            "files": [item.to_dict() for item in self.files],
        }

    def validate(self) -> None:
        """Reject malformed requests before opening files or contacting a service."""
        if not self.task.strip():
            raise ValueError("video task must not be empty")
        if not self.sample_id.strip():
            raise ValueError("video dataset id must not be empty")
        if not self.prompt.strip():
            raise ValueError(f"video dataset sample {self.sample_id!r} has no prompt")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("video width and height must be positive")
        if self.num_frames <= 0 or self.num_inference_steps <= 0:
            raise ValueError("video frames and inference steps must be positive")
        if self.fps <= 0:
            raise ValueError("video fps must be positive")
        for item in self.files:
            if not item.field.strip():
                raise ValueError("video multipart file field must not be empty")
            if not Path(item.path).is_file():
                raise ValueError(f"video input file does not exist: {item.path}")


@dataclass(frozen=True)
class VideoParameterSweepConfig:
    """Store JSONL sweep settings for video generation points."""

    path: str = ""
    num_runs: int = 1
    experiment_name: str = ""


@dataclass(frozen=True)
class VideoEndpointConfig:
    """Describe the synchronous endpoint used by a video request client."""

    url: str
    health_url: str
    timeout_s: float = 3600.0


@dataclass(frozen=True)
class VideoBenchmarkConfig:
    """Join an endpoint and output settings with dataset-owned video requests."""

    name: str
    dataset_source: str
    dataset_path: str
    endpoint: VideoEndpointConfig
    requests: tuple[VideoGenerationRequest, ...]
    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig
    dataset_offset: int = 0
    concurrency: int = 1
    warmup_requests: int = 0
    duration_s: float | None = None
    sweep: VideoParameterSweepConfig = field(default_factory=VideoParameterSweepConfig)

    def validate(self) -> None:
        """Validate the complete benchmark before sending any request."""
        if not self.requests:
            raise ValueError("video dataset contains no requests")
        if self.dataset_offset < 0:
            raise ValueError("video dataset offset must be zero or positive")
        if self.concurrency <= 0:
            raise ValueError("video --max-concurrency must be positive")
        if self.warmup_requests < 0:
            raise ValueError("video --warmup-requests must be zero or positive")
        if self.warmup_requests > len(self.requests):
            raise ValueError("video --warmup-requests cannot exceed selected requests")
        if self.duration_s is not None and self.duration_s <= 0:
            raise ValueError("video --duration must be positive")
        if self.sweep.num_runs < 1:
            raise ValueError("video --num-runs must be positive")
        endpoint = urlsplit(self.endpoint.url)
        health = urlsplit(self.endpoint.health_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError(f"invalid video endpoint URL: {self.endpoint.url}")
        if health.scheme not in {"http", "https"} or not health.netloc:
            raise ValueError(f"invalid video health URL: {self.endpoint.health_url}")
        if self.endpoint.timeout_s <= 0:
            raise ValueError("video --timeout must be positive")
        self.outputs.validate()
        if self.outputs.includes("wandb") and not self.wandb.project.strip():
            raise ValueError("video --wandb-project must not be empty")
        sample_ids: set[str] = set()
        for request in self.requests:
            request.validate()
            if request.sample_id in sample_ids:
                raise ValueError(f"duplicate video dataset id: {request.sample_id}")
            sample_ids.add(request.sample_id)

    def to_dict(self) -> dict[str, Any]:
        """Return the benchmark configuration persisted with each result."""
        return {
            "name": self.name,
            "dataset_source": self.dataset_source,
            "dataset_path": self.dataset_path,
            "dataset_offset": self.dataset_offset,
            "request_count": len(self.requests),
            "tasks": sorted({request.task for request in self.requests}),
            "requests": [request.to_dict() for request in self.requests],
            "endpoint": {
                "url": self.endpoint.url,
                "health_url": self.endpoint.health_url,
                "timeout_s": self.endpoint.timeout_s,
            },
            "concurrency": self.concurrency,
            "warmup_requests": self.warmup_requests,
            "duration_s": self.duration_s,
            "output": {
                "destinations": list(self.outputs.destinations),
                "output_dir": self.outputs.output_dir,
            },
            "wandb": {
                "project": self.wandb.project,
                "entity": self.wandb.entity,
                "group": self.wandb.group,
                "run_name": self.wandb.run_name,
            },
            "sweep": {
                "path": self.sweep.path,
                "num_runs": self.sweep.num_runs,
                "experiment_name": self.sweep.experiment_name,
            },
        }


def video_health_url(endpoint_url: str) -> str:
    """Derive the conventional health endpoint from the video endpoint URL."""
    parsed = urlsplit(endpoint_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
