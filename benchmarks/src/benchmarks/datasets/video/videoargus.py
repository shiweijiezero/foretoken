# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve VideoArgusBench task data and normalize its provider-specific media contract."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download
from loguru import logger

from benchmarks.config.video import (
    VideoDatasetDefaults,
    VideoGenerationRequest,
    VideoInputFile,
)

_VIDEOARGUS_TASKS = {
    "T2V": "t2va",
    "TI2V": "fl2va",
    "TS2V": "ref2va",
    "TV2V": "ref2va",
    "TSV2V": "ref2va",
}


_VIDEOARGUS_ALIAS = "VideoArgusBench"


_VIDEOARGUS_REPO_ID = "zengziyun/VideoArgusBench"


def resolve_video_dataset(path_or_selector: str) -> str:
    """Resolve a local JSONL path or download a VideoArgusBench task."""
    selector_task = _videoargus_selector(path_or_selector)
    if selector_task is None:
        return str(Path(path_or_selector).expanduser().resolve())

    local_manifest = _local_videoargus_manifest(selector_task)
    if local_manifest is not None:
        logger.info(
            "Resolved {}/{} below FORETOKEN_DATA_ROOT",
            _VIDEOARGUS_ALIAS,
            selector_task,
        )
        return str(local_manifest.absolute())

    data_root = _configured_data_root()
    cache_dir = data_root / "huggingface" / "hub" if data_root else None
    logger.info(
        "Resolving {}/{} through the Hugging Face cache{}",
        _VIDEOARGUS_ALIAS,
        selector_task,
        f" at {cache_dir}" if cache_dir else "",
    )
    snapshot_path = Path(
        snapshot_download(
            repo_id=_VIDEOARGUS_REPO_ID,
            repo_type="dataset",
            cache_dir=str(cache_dir) if cache_dir else None,
            allow_patterns=[
                f"{selector_task}/**",
                "README.md",
                ".gitattributes",
            ],
            max_workers=1,
        )
    )
    manifest_path = snapshot_path / selector_task / "manifest.jsonl"
    if not _videoargus_task_complete(manifest_path):
        raise RuntimeError(
            f"VideoArgusBench/{selector_task} download is incomplete: "
            f"{manifest_path}"
        )
    # Keep the Hub snapshot path intact. Hugging Face represents snapshot files
    # as symlinks into ``blobs/``; resolving manifest.jsonl would therefore make
    # its relative media paths point at the blob store instead of the task dir.
    return str(manifest_path.absolute())


def _videoargus_request(
    raw: Any,
    *,
    line_number: int,
    row_index: int,
    dataset_dir: Path,
    defaults: VideoDatasetDefaults,
) -> VideoGenerationRequest:
    """Convert one VideoArgusBench manifest row to a video request."""
    if not isinstance(raw, dict):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"VideoArgus line {line_number} must be a JSON object"
        )
    task_name = raw.get("task")
    if task_name not in _VIDEOARGUS_TASKS:
        raise ValueError(
            f"VideoArgus line {line_number} has unsupported task: {task_name!r}"
        )
    sample_id = raw.get("id")
    prompt = raw.get("text")
    media = raw.get("media", [])
    if not isinstance(sample_id, str) or not isinstance(prompt, str):
        raise ValueError(  # noqa: TRY004 - preserve the CLI's dataset error boundary.
            f"VideoArgus line {line_number} requires string id and text fields"
        )
    if not isinstance(media, list) or any(
        not isinstance(item, str) for item in media
    ):
        raise ValueError(
            f"VideoArgus line {line_number} media must be a list of paths"
        )

    paths = [
        _videoargus_media_path(dataset_dir, item, line_number=line_number)
        for item in media
    ]
    content_types = [
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        for path in paths
    ]
    if task_name == "T2V" and paths:
        raise ValueError(f"VideoArgus T2V line {line_number} must not have media")
    if task_name == "TI2V":
        if len(paths) != 1 or not content_types[0].startswith("image/"):
            raise ValueError(
                f"VideoArgus TI2V line {line_number} requires one image"
            )
        frame_indices = (0,)
    else:
        frame_indices = ()
    if task_name in {"TS2V", "TV2V", "TSV2V"} and not paths:
        raise ValueError(
            f"VideoArgus {task_name} line {line_number} requires reference media"
        )
    if task_name in {"TS2V", "TV2V", "TSV2V"}:
        image_count = sum(value.startswith("image/") for value in content_types)
        video_count = sum(value.startswith("video/") for value in content_types)
        audio_count = sum(value.startswith("audio/") for value in content_types)
        if image_count + video_count + audio_count != len(content_types):
            raise ValueError(
                f"VideoArgus {task_name} line {line_number} has unsupported media"
            )
        if len(paths) > 12 or image_count > 9 or video_count > 3 or audio_count > 3:
            raise ValueError(
                f"VideoArgus {task_name} line {line_number} exceeds reference limits"
            )
        if audio_count and not image_count and not video_count:
            raise ValueError(
                f"VideoArgus {task_name} line {line_number} requires visual media with audio"
            )

    # The vLLM-Omni endpoint decodes a singular upload into an in-memory image
    # or video frame list. Ref2VA instead needs uploaded videos preserved as
    # paths, which is the plural upload contract even for one video.
    singular_image = len(paths) == 1 and content_types[0].startswith("image/")
    field = (
        "input_reference"
        if task_name == "TI2V" or singular_image
        else "input_references"
    )
    request = VideoGenerationRequest(
        sample_id=sample_id,
        task=_VIDEOARGUS_TASKS[task_name],
        prompt=prompt,
        width=defaults.width,
        height=defaults.height,
        num_frames=defaults.num_frames,
        fps=defaults.fps,
        num_inference_steps=defaults.num_inference_steps,
        aspect_ratio=None if task_name == "TI2V" else defaults.aspect_ratio,
        flow_shift=defaults.flow_shift,
        audio_flow_shift=defaults.audio_flow_shift,
        seed=defaults.seed + row_index,
        frame_indices=frame_indices,
        files=tuple(
            VideoInputFile(field=field, path=str(path), content_type=content_type)
            for path, content_type in zip(paths, content_types)
        ),
    )
    request.validate()
    return request


def _videoargus_selector(value: str) -> str | None:
    """Return the normalized task from ``VideoArgusBench/<task>``."""
    parts = value.strip().split("/")
    if len(parts) != 2 or parts[0].lower() != _VIDEOARGUS_ALIAS.lower():
        return None
    task_name = parts[1].upper()
    if task_name not in _VIDEOARGUS_TASKS:
        choices = ", ".join(_VIDEOARGUS_TASKS)
        raise ValueError(
            f"unsupported VideoArgusBench task {parts[1]!r}; choose: {choices}"
        )
    return task_name


def _videoargus_media_path(
    dataset_dir: Path,
    value: str,
    *,
    line_number: int | None = None,
) -> Path:
    """Return a task-local media path without resolving Hub snapshot symlinks."""
    relative_path = Path(os.path.expandvars(os.path.expanduser(value)))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        location = f" on line {line_number}" if line_number is not None else ""
        raise ValueError(
            f"VideoArgus media path{location} must stay inside its task "
            f"directory: {value!r}"
        )
    return (dataset_dir / relative_path).absolute()


def _videoargus_task_complete(manifest_path: Path) -> bool:
    """Return whether a local task manifest and all of its media are present."""
    if not manifest_path.is_file():
        return False
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                media = row.get("media", []) if isinstance(row, dict) else []
                if not isinstance(media, list) or any(
                    not isinstance(item, str) for item in media
                ):
                    return False
                if any(
                    not _videoargus_media_path(manifest_path.parent, item).is_file()
                    for item in media
                ):
                    return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def _configured_data_root() -> Path | None:
    """Return the configured root that owns local and downloaded benchmark data."""
    configured = os.environ.get("FORETOKEN_DATA_ROOT")
    if not configured:
        return None
    return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()


def _local_videoargus_manifest(task_name: str) -> Path | None:
    """Return a complete VideoArgus task below FORETOKEN_DATA_ROOT, if present."""
    data_root = _configured_data_root()
    if data_root is None:
        return None
    manifest_path = data_root / _VIDEOARGUS_ALIAS / task_name / "manifest.jsonl"
    return manifest_path if _videoargus_task_complete(manifest_path) else None
