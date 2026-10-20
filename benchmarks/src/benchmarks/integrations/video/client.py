# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Multipart transport and metric normalization for video generation."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

import httpx

from benchmarks.config.video import VideoBenchmarkConfig, VideoGenerationRequest
from benchmarks.integrations.video.probe import (
    _resolve_ffprobe,
    probe_video,
    validate_video_probe,
)
from benchmarks.integrations.video.types import VideoSampleResult


class VideoGenerationClient:
    """Send dataset-owned requests to one synchronous video endpoint."""

    def __init__(self, config: VideoBenchmarkConfig):
        self.config = config
        self._ffprobe_executable: str | None = None
        self._ffprobe_warning: str | None = None
        self._ffprobe_prepared = False
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.endpoint.timeout_s),
            follow_redirects=True,
        )

    async def prepare_video_validation(self) -> str | None:
        """Prepare ffprobe once and return why metadata validation is disabled."""
        if not self._ffprobe_prepared:
            self._ffprobe_executable, self._ffprobe_warning = (
                await _resolve_ffprobe()
            )
            self._ffprobe_prepared = True
        return self._ffprobe_warning

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self.client.aclose()

    async def generate(
        self,
        request: VideoGenerationRequest,
        index: int,
        output_path: Path | None,
    ) -> VideoSampleResult:
        """Generate, persist, probe, and measure one dataset request."""
        started = time.perf_counter()
        result_identity = {
            "sample_id": request.sample_id,
            "task": request.task,
            "width": request.width,
            "height": request.height,
            "num_frames": request.num_frames,
            "fps": request.fps,
            "num_inference_steps": request.num_inference_steps,
            "seed": request.seed,
            "prompt": request.prompt,
        }
        part_path: Path | None = None
        try:
            with ExitStack() as stack:
                files = []
                for item in request.files:
                    # HTTPX multipart requires a request-scoped synchronous file object.
                    stream = stack.enter_context(
                        open(  # noqa: ASYNC230 - HTTPX consumes this sync stream.
                            item.path, "rb"
                        )
                    )
                    files.append(
                        (
                            item.field,
                            (Path(item.path).name, stream, item.content_type),
                        )
                    )
                async with self.client.stream(
                    "POST",
                    self.config.endpoint.url,
                    data=video_request_form_data(request),
                    files=files,
                ) as response:
                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", "replace")
                        return VideoSampleResult(
                            index=index,
                            success=False,
                            status_code=response.status_code,
                            error=body[:4000],
                            client_e2e_s=time.perf_counter() - started,
                            **result_identity,
                        )
                    written = 0
                    part_path = (
                        output_path.with_suffix(output_path.suffix + ".part")
                        if output_path is not None
                        else None
                    )
                    output_stream = (
                        stack.enter_context(
                            open(  # noqa: ASYNC230 - request-scoped output stream.
                                part_path, "wb"
                            )
                        )
                        if part_path is not None
                        else None
                    )
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if output_stream is not None:
                            output_stream.write(chunk)
                    if output_stream is not None:
                        output_stream.flush()
                    headers = response.headers
                    status_code = response.status_code
        except (httpx.HTTPError, OSError) as exc:
            if part_path is not None:
                with suppress(OSError):
                    part_path.unlink(missing_ok=True)
            return VideoSampleResult(
                index=index,
                success=False,
                status_code=None,
                error=f"{type(exc).__name__}: {exc}",
                client_e2e_s=time.perf_counter() - started,
                **result_identity,
            )

        # Decode the server contract before publishing or probing the completed media.
        metadata = _response_metrics(
            headers, client_e2e_s=time.perf_counter() - started,
            steps=request.num_inference_steps,
        )
        if part_path is not None and written > 0:
            part_path.replace(output_path)
        elif part_path is not None:
            part_path.unlink(missing_ok=True)
        output_probe: dict[str, Any] = {}
        validation_error = None
        if output_path is not None and output_path.is_file():
            if not self._ffprobe_prepared:
                await self.prepare_video_validation()
            if self._ffprobe_executable is None:
                assert self._ffprobe_warning is not None
                output_probe = {"warning": self._ffprobe_warning}
            else:
                output_probe = await probe_video(
                    output_path, self._ffprobe_executable
                )
            validation_error = validate_video_probe(
                output_probe,
                width=request.width,
                height=request.height,
                num_frames=request.num_frames,
            )
        return VideoSampleResult(
            index=index,
            success=written > 0 and validation_error is None,
            status_code=status_code,
            error=(
                validation_error
                if validation_error is not None
                else None
                if written > 0
                else "video response body was empty"
            ),
            request_id=headers.get("x-request-id", ""),
            model=headers.get("x-model", ""),
            output_path=str(output_path) if output_path is not None else None,
            output_bytes=written,
            output_probe=output_probe,
            **result_identity,
            **metadata,
        )


def normalize_stage_metrics(
    raw: dict[str, float], *, e2e_s: float, steps: int
) -> dict[str, float | None]:
    """Map vLLM-Omni profiler names to stable video benchmark stages."""
    queue_wait_ms = raw.get("queue_wait_ms")
    stage_generation_ms = raw.get("stage_0_gen_ms")
    forward = [
        value
        for key, value in raw.items()
        if key.endswith("Pipeline.forward") and "text_encoder" not in key
    ]
    pipeline_s = max(forward) if forward else None
    encode_s = _stage_sum(raw, (".encode_prompt",))
    if encode_s is None:
        encode_s = _stage_sum(raw, ("text_encoder.forward",))
    media_encode_s = _stage_sum(
        raw,
        (
            "._encode_local_media",
            "._encode_video_conditions",
            "._encode_video_audio_conditions",
            "._encode_visual_condition",
            "._encode_audio_condition",
            "vae.encode",
        ),
    )
    denoise_s = _stage_sum(raw, (".diffuse",))
    decode_s = _stage_sum(raw, (".decode",))
    direct_preprocess_s = _stage_sum(
        raw,
        (
            "._prepare_reference_videos",
            "._prepare_reference_images",
            "._prepare_inputs",
        ),
    )
    known_pipeline = sum(
        value
        for value in (encode_s, media_encode_s, denoise_s, decode_s)
        if value is not None
    )
    if pipeline_s is not None:
        preprocess_s = max(0.0, pipeline_s - known_pipeline)
        postprocess_s = max(0.0, e2e_s - pipeline_s)
    else:
        preprocess_s = direct_preprocess_s
        postprocess_s = None
    return {
        "queue_wait_s": (
            queue_wait_ms / 1000.0 if queue_wait_ms is not None else None
        ),
        "server_generation_s": (
            stage_generation_ms / 1000.0
            if stage_generation_ms is not None
            else raw.get("ar_stage_0")
        ),
        "preprocess_s": preprocess_s,
        "encode_s": encode_s,
        "media_encode_s": media_encode_s,
        "denoise_s": denoise_s,
        "decode_s": decode_s,
        "postprocess_s": postprocess_s,
        "denoise_per_step_s": (
            denoise_s / steps if denoise_s is not None and steps else None
        ),
    }


def video_request_form_data(request: VideoGenerationRequest) -> dict[str, str]:
    """Map one normalized request to the current multipart endpoint contract."""
    values: dict[str, Any] = {
        "prompt": request.prompt,
        "width": request.width,
        "height": request.height,
        "num_frames": request.num_frames,
        "fps": request.fps,
        "num_inference_steps": request.num_inference_steps,
        "aspect_ratio": request.aspect_ratio,
        "flow_shift": request.flow_shift,
        "seed": request.seed,
        "extra_params": {
            "task": request.task,
            **(
                {"audio_flow_shift": request.audio_flow_shift}
                if request.audio_flow_shift is not None
                else {}
            ),
            **(
                {"frame_indices": list(request.frame_indices)}
                if request.frame_indices
                else {}
            ),
        },
    }
    return {
        key: json.dumps(value, separators=(",", ":"))
        if isinstance(value, dict)
        else str(value)
        for key, value in values.items()
        if value is not None
    }


def _response_metrics(
    headers: httpx.Headers, *, client_e2e_s: float, steps: int,
) -> dict[str, Any]:
    """Decode response timing and resource metadata for a completed video transport."""
    server_e2e_s = _float_header(headers, "x-inference-time-s")
    e2e_s = server_e2e_s if server_e2e_s is not None else client_e2e_s
    raw_stages: dict[str, float] = {}
    try:
        parsed = json.loads(headers.get("x-stage-durations", "{}"))
        if isinstance(parsed, dict):
            raw_stages = {
                str(key): float(value)
                for key, value in parsed.items()
                if isinstance(value, (int, float))
            }
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_stages = {}
    stage_reference_tokens = _pop_reference_tokens(raw_stages)
    normalized = normalize_stage_metrics(raw_stages, e2e_s=e2e_s, steps=steps)
    reference_tokens = None
    reference_source = "unavailable"
    header_tokens = headers.get("x-reference-tokens")
    if header_tokens:
        try:
            reference_tokens = int(header_tokens)
            reference_source = "response_header"
        except ValueError:
            pass
    if reference_tokens is None and stage_reference_tokens is not None:
        reference_tokens = stage_reference_tokens
        reference_source = "stage_metadata"
    peak_gpu_memory_mb = _float_header(headers, "x-peak-memory-mb")
    if peak_gpu_memory_mb is not None and peak_gpu_memory_mb <= 0:
        peak_gpu_memory_mb = None
    return {
        "client_e2e_s": client_e2e_s, "e2e_s": e2e_s,
        "peak_gpu_memory_mb": peak_gpu_memory_mb,
        "reference_tokens": reference_tokens, "reference_tokens_source": reference_source,
        "raw_stage_durations": raw_stages, **normalized,
    }


def _float_header(headers: httpx.Headers, name: str) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _stage_sum(stages: dict[str, float], suffixes: tuple[str, ...]) -> float | None:
    values = [
        value
        for key, value in stages.items()
        if not key.endswith("_ms") and any(key.endswith(suffix) for suffix in suffixes)
    ]
    return sum(values) if values else None


def _pop_reference_tokens(stages: dict[str, float]) -> int | None:
    """Remove and return reference-media token metadata from stage metrics."""
    for key in tuple(stages):
        if not key.endswith(".reference_tokens"):
            continue
        value = stages.pop(key)
        if value >= 0 and value.is_integer():
            return int(value)
    return None
