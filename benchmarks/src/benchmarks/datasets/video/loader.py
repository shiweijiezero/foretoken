# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""JSONL and VideoArgusBench loading for video-generation benchmarks."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from benchmarks.config.video import (
    VideoDatasetDefaults,
    VideoGenerationRequest,
    VideoInputFile,
)
from benchmarks.datasets.video.videoargus import (
    _VIDEOARGUS_TASKS,
    _videoargus_request,
    resolve_video_dataset,
)


class _VideoFileRow(BaseModel):
    """Decode the explicit multipart-file fields accepted by native video JSONL."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    field: str
    path: str
    content_type: str | None = None


class _VideoRequestRow(BaseModel):
    """Decode native video rows before resolving files or applying request-domain rules."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    id: str
    task: str
    prompt: str
    width: int
    height: int
    num_frames: int
    num_inference_steps: int
    fps: int = VideoDatasetDefaults.fps
    aspect_ratio: str | None = None
    flow_shift: float | None = None
    audio_flow_shift: float | None = None
    seed: int | None = None
    frame_indices: list[int] = Field(default_factory=list)
    files: list[_VideoFileRow] = Field(default_factory=list)


def load_video_dataset(
    path: str,
    *,
    defaults: VideoDatasetDefaults | None = None,
    number: int | None = 0,
    offset: int = 0,
) -> tuple[str, tuple[VideoGenerationRequest, ...]]:
    """Load native video JSONL or a VideoArgusBench manifest."""
    if number < 0:
        raise ValueError("video --num-prompts must be zero or positive")
    if offset < 0:
        raise ValueError("video --dataset-offset must be zero or positive")
    dataset_path = Path(resolve_video_dataset(path))
    if not dataset_path.is_file():
        raise ValueError(f"video dataset does not exist: {dataset_path}")
    public_defaults = defaults or VideoDatasetDefaults()
    requests: list[VideoGenerationRequest] = []
    row_index = 0
    with dataset_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            current_index = row_index
            row_index += 1
            if current_index < offset:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON on video dataset line {line_number}: {exc.msg}"
                ) from exc
            if (
                isinstance(raw, dict)
                and raw.get("task") in _VIDEOARGUS_TASKS
                and "text" in raw
            ):
                requests.append(
                    _videoargus_request(
                        raw,
                        line_number=line_number,
                        row_index=current_index,
                        dataset_dir=dataset_path.parent,
                        defaults=public_defaults,
                    )
                )
            else:
                requests.append(
                    _load_request(
                        raw,
                        line_number=line_number,
                        dataset_dir=dataset_path.parent,
                    )
                )
            if number and len(requests) >= number:
                break
    if number and len(requests) < number:
        raise ValueError(
            f"loaded {len(requests)} video requests from {dataset_path} "
            f"(offset={offset}), need {number}"
        )
    return str(dataset_path), tuple(requests)


def video_dataset_name(path: str) -> str:
    """Return a useful run name for native or VideoArgus JSONL inputs."""
    dataset_path = Path(path).expanduser().absolute()
    if (
        dataset_path.name in {"manifest.jsonl", "input.jsonl"}
        and dataset_path.parent.name in _VIDEOARGUS_TASKS
    ):
        return f"videoargus_{dataset_path.parent.name.lower()}"
    return dataset_path.stem


def _load_request(
    raw: Any, *, line_number: int, dataset_dir: Path
) -> VideoGenerationRequest:
    """Validate one native dataset row and resolve its multipart files relative to the manifest."""
    try:
        row = _VideoRequestRow.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid video dataset line {line_number}: {error}") from error
    files: list[VideoInputFile] = []
    for item in row.files:
        path = Path(os.path.expandvars(os.path.expanduser(item.path)))
        if not path.is_absolute():
            path = (dataset_dir / path).resolve()
        files.append(VideoInputFile(
            field=item.field,
            path=str(path),
            content_type=item.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        ))
    request = VideoGenerationRequest(
        sample_id=row.id,
        task=row.task.lower(),
        prompt=row.prompt,
        width=row.width,
        height=row.height,
        num_frames=row.num_frames,
        fps=row.fps,
        num_inference_steps=row.num_inference_steps,
        aspect_ratio=row.aspect_ratio,
        flow_shift=row.flow_shift,
        audio_flow_shift=row.audio_flow_shift,
        seed=row.seed,
        frame_indices=tuple(row.frame_indices),
        files=tuple(files),
    )
    request.validate()
    return request
