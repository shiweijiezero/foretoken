# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Request execution and result persistence for video benchmarks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoGenerationClient, VideoSampleResult
from benchmarks.results.output import ResultOutputs
from benchmarks.results.video import (
    create_video_benchmark_run,
    video_result_sinks,
    video_run_record,
)
from benchmarks.results.video_wandb import VideoWandbError

logger = logging.getLogger(__name__)


class VideoBenchmarkError(RuntimeError):
    """Report a video benchmark failure that must make the command fail."""


async def _health_ok(url: str) -> bool:
    """Return whether the configured external service is ready for requests."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        return response.is_success
    except httpx.HTTPError:
        return False


async def _run_requests(
    config: VideoBenchmarkConfig, run_dir: Path
) -> list[VideoSampleResult]:
    """Send every dataset row with the configured concurrency limit."""
    client = VideoGenerationClient(config)
    semaphore = asyncio.Semaphore(config.concurrency)

    async def one(index: int) -> VideoSampleResult:
        request = config.requests[index]
        async with semaphore:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.sample_id).strip(
                ".-"
            )
            output = run_dir / f"video_{index:04d}_{safe_id or 'sample'}.mp4"
            return await client.generate(request, index, output)

    try:
        ffprobe_warning = await client.prepare_video_validation()
        if ffprobe_warning is not None:
            logger.warning(
                "Generated video metadata validation is disabled: %s",
                ffprobe_warning,
            )
        return await asyncio.gather(
            *(one(index) for index in range(len(config.requests)))
        )
    finally:
        await client.close()


async def run_video_benchmark(
    config: VideoBenchmarkConfig, *, dry_run: bool = False
) -> dict[str, Any]:
    """Send video requests and persist generated media, samples, and metrics."""
    if dry_run:
        logger.info(
            "Video dry run validated config:\n%s",
            json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        )
        return {"metrics": {"request_num": 0, "success_num": 0}, "dry_run": True}

    if not await _health_ok(config.endpoint.health_url):
        raise VideoBenchmarkError(
            f"Video endpoint is not healthy: {config.endpoint.health_url}. "
            "Start the video service before running the benchmark."
        )
    logger.info("Video server is healthy: %s", config.endpoint.health_url)

    record = video_run_record(config)
    output_dir = None
    execution_dir = None
    outputs = ResultOutputs(
        config,
        None,
        record,
        directory_prefix=f"{config.name}_",
        sink_factory=partial(video_result_sinks, config),
    )
    try:
        with outputs:
            execution_dir = outputs.execution_dir
            run_dir = Path(execution_dir)
            results = await _run_requests(config, run_dir)
            run = create_video_benchmark_run(
                record,
                results,
                run_dir,
            )
            outputs.publish(run)
            if config.outputs.includes("local"):
                output_dir = execution_dir
    except VideoWandbError as exc:
        preserved = (
            f"; artifacts preserved at {execution_dir}"
            if execution_dir is not None and Path(execution_dir).is_dir()
            else ""
        )
        raise VideoBenchmarkError(f"{exc}{preserved}") from exc
    return {"metrics": run.metrics, "output_dir": output_dir}
