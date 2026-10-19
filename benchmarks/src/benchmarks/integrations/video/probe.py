# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Inspect generated media with the native ffprobe subprocess."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any


async def probe_video(path: Path, executable: str = "ffprobe") -> dict[str, Any]:
    """Return ffprobe metadata or a structured media validation error."""
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate,nb_read_frames:"
            "format=duration,size",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {
            "error": (
                "ffprobe could not start after its availability check: "
                f"{exc}"
            )
        }
    stdout, stderr = await _read_probe_output(process)
    if process.returncode:
        message = stderr.decode("utf-8", "replace").strip()
        detail = message or f"exit status {process.returncode}"
        return {"error": f"ffprobe failed: {detail}"}
    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "ffprobe returned invalid JSON"}
    if not isinstance(metadata, dict):
        return {"error": "ffprobe returned non-object JSON"}
    return metadata


def validate_video_probe(
    probe: dict[str, Any], *, width: int, height: int, num_frames: int
) -> str | None:
    """Return a useful error when generated video metadata is unexpected."""
    if probe.get("error"):
        return str(probe["error"])
    streams = probe.get("streams")
    if not isinstance(streams, list) or not streams:
        return None if probe.get("warning") else "ffprobe found no video stream"
    stream = streams[0]
    observed_width = int(stream.get("width") or 0)
    observed_height = int(stream.get("height") or 0)
    observed_frames = int(stream.get("nb_read_frames") or 0)
    if (observed_width, observed_height) != (width, height):
        return (
            f"generated resolution is {observed_width}x{observed_height}, "
            f"expected {width}x{height}"
        )
    if observed_frames and observed_frames != num_frames:
        return f"generated frame count is {observed_frames}, expected {num_frames}"
    return None


async def _resolve_ffprobe() -> tuple[str | None, str | None]:
    """Resolve and launch-check ffprobe once for a video client run."""
    executable = shutil.which("ffprobe")
    if executable is None:
        return None, "ffprobe is not installed"
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"ffprobe could not start: {exc}"
    _, stderr = await _read_probe_output(process)
    if process.returncode:
        message = stderr.decode("utf-8", "replace").strip()
        detail = message or f"exit status {process.returncode}"
        return None, f"ffprobe is unavailable: {detail}"
    return executable, None


async def _read_probe_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes | None, bytes | None]:
    """Read an owned probe's pipes and reap it, terminating the child when the caller is cancelled."""
    output = asyncio.create_task(process.communicate())
    try:
        return await asyncio.shield(output)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # Keep both pipe readers alive until the child exits, including full stderr pipes.
        await output
