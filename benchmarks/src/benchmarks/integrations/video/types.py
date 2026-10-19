# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Carry normalized video completion evidence between transport, measurement, and recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VideoSampleResult:
    """Record request identity, validation, timings, and generated artifacts."""

    index: int
    success: bool
    status_code: int | None
    error: str | None
    sample_id: str = ""
    task: str = ""
    width: int = 0
    height: int = 0
    num_frames: int = 0
    fps: int = 0
    num_inference_steps: int = 0
    seed: int | None = None
    request_id: str = ""
    model: str = ""
    prompt: str = ""
    client_e2e_s: float = 0.0
    e2e_s: float = 0.0
    queue_wait_s: float | None = None
    server_generation_s: float | None = None
    preprocess_s: float | None = None
    encode_s: float | None = None
    media_encode_s: float | None = None
    denoise_s: float | None = None
    decode_s: float | None = None
    postprocess_s: float | None = None
    denoise_per_step_s: float | None = None
    peak_gpu_memory_mb: float | None = None
    reference_tokens: int | None = None
    reference_tokens_source: str = "unavailable"
    output_path: str | None = None
    output_bytes: int = 0
    output_probe: dict[str, Any] = field(default_factory=dict)
    raw_stage_durations: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation consumed by reports."""
        return {
            "index": self.index,
            "sample_id": self.sample_id,
            "task": self.task,
            "prompt": self.prompt,
            "width": self.width,
            "height": self.height,
            "num_frames": self.num_frames,
            "fps": self.fps,
            "num_inference_steps": self.num_inference_steps,
            "seed": self.seed,
            "success": self.success,
            "status_code": self.status_code,
            "error": self.error,
            "request_id": self.request_id,
            "model": self.model,
            "client_e2e_s": self.client_e2e_s,
            "e2e_s": self.e2e_s,
            "queue_wait_s": self.queue_wait_s,
            "server_generation_s": self.server_generation_s,
            "preprocess_s": self.preprocess_s,
            "encode_s": self.encode_s,
            "media_encode_s": self.media_encode_s,
            "denoise_s": self.denoise_s,
            "decode_s": self.decode_s,
            "postprocess_s": self.postprocess_s,
            "denoise_per_step_s": self.denoise_per_step_s,
            "peak_gpu_memory_mb": self.peak_gpu_memory_mb,
            "reference_tokens": self.reference_tokens,
            "reference_tokens_source": self.reference_tokens_source,
            "output_path": self.output_path,
            "output_bytes": self.output_bytes,
            "output_probe": self.output_probe,
            "raw_stage_durations": self.raw_stage_durations,
        }
