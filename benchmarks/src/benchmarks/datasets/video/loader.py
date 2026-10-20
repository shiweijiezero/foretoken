# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""JSONL and VideoArgusBench loading for video-generation benchmarks."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

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

logger = logging.getLogger(__name__)


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
    if not isinstance(raw, dict):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"video dataset line {line_number} must be a JSON object"
        )
    allowed = {
        "id",
        "task",
        "prompt",
        "width",
        "height",
        "num_frames",
        "fps",
        "num_inference_steps",
        "aspect_ratio",
        "flow_shift",
        "audio_flow_shift",
        "seed",
        "frame_indices",
        "files",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"video dataset line {line_number} has unknown fields: "
            f"{', '.join(sorted(unknown))}"
        )
    required = {
        "id",
        "task",
        "prompt",
        "width",
        "height",
        "num_frames",
        "num_inference_steps",
    }
    missing = required - set(raw)
    if missing:
        raise ValueError(
            f"video dataset line {line_number} is missing: "
            f"{', '.join(sorted(missing))}"
        )
    for name in ("id", "task", "prompt"):
        if not isinstance(raw[name], str):
            raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
                f"video dataset line {line_number} has invalid {name}: {raw[name]!r}"
            )
    if raw.get("aspect_ratio") is not None and not isinstance(
        raw["aspect_ratio"], str
    ):
        raise ValueError(
            f"video dataset line {line_number} has invalid aspect_ratio: "
            f"{raw['aspect_ratio']!r}"
        )
    files: list[VideoInputFile] = []
    file_rows = raw.get("files", [])
    if not isinstance(file_rows, list):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"video dataset line {line_number} files must be a list"
        )
    for item in file_rows:
        if not isinstance(item, dict) or not {"field", "path"} <= set(item):
            raise ValueError(
                f"video dataset line {line_number} file entries require field and path"
            )
        unknown_file_fields = set(item) - {"field", "path", "content_type"}
        if unknown_file_fields:
            raise ValueError(
                f"video dataset line {line_number} file has unknown fields: "
                f"{', '.join(sorted(unknown_file_fields))}"
            )
        path = Path(os.path.expandvars(os.path.expanduser(str(item["path"]))))
        if not path.is_absolute():
            path = (dataset_dir / path).resolve()
        files.append(
            VideoInputFile(
                field=str(item["field"]),
                path=str(path),
                content_type=str(
                    item.get("content_type")
                    or mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream"
                ),
            )
        )
    frame_indices_raw = raw.get("frame_indices", [])
    if not isinstance(frame_indices_raw, list):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"video dataset line {line_number} frame_indices must be a list"
        )
    num_frames = _row_int(raw["num_frames"], "num_frames", line_number)
    request = VideoGenerationRequest(
        sample_id=raw["id"],
        task=raw["task"].lower(),
        prompt=raw["prompt"],
        width=_row_int(raw["width"], "width", line_number),
        height=_row_int(raw["height"], "height", line_number),
        num_frames=num_frames,
        fps=_row_int(raw.get("fps", 24), "fps", line_number),
        num_inference_steps=_row_int(
            raw["num_inference_steps"], "num_inference_steps", line_number
        ),
        aspect_ratio=raw.get("aspect_ratio"),
        flow_shift=(
            _row_float(raw["flow_shift"], "flow_shift", line_number)
            if raw.get("flow_shift") is not None
            else None
        ),
        audio_flow_shift=(
            _row_float(raw["audio_flow_shift"], "audio_flow_shift", line_number)
            if raw.get("audio_flow_shift") is not None
            else None
        ),
        seed=(
            _row_int(raw["seed"], "seed", line_number)
            if raw.get("seed") is not None
            else None
        ),
        frame_indices=tuple(
            _row_int(value, "frame_indices", line_number)
            for value in frame_indices_raw
        ),
        files=tuple(files),
    )
    request.validate()
    return request


def _row_int(value: Any, name: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"video dataset line {line_number} has invalid {name}: {value!r}"
        )
    return value


def _row_float(value: Any, name: str, line_number: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"video dataset line {line_number} has invalid {name}: {value!r}"
        )
    return float(value)
