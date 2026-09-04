# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Request execution and result persistence for video benchmarks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any

import httpx

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoGenerationClient, VideoSampleResult
from benchmarks.results.output import write_json
from benchmarks.results.video import aggregate_video_results, log_video_summary
from benchmarks.results.video_wandb import publish_video_to_wandb

logger = logging.getLogger(__name__)


class VideoBenchmarkError(RuntimeError):
    """Report an endpoint failure that prevents request execution."""


@contextmanager
def _video_execution_directory(
    config: VideoBenchmarkConfig,
) -> Iterator[Path]:
    """Provide a persistent local directory or a temporary working directory."""
    if config.outputs.includes("local"):
        root = Path(config.outputs.output_dir)
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        yield Path(
            mkdtemp(prefix=f"{config.name}_{timestamp}-", dir=root)
        )
        return

    with TemporaryDirectory(prefix="foretoken-video-benchmark-") as directory:
        yield Path(directory)


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

    with _video_execution_directory(config) as run_dir:
        execution_dir = str(run_dir)
        write_json(execution_dir, "config.json", config.to_dict())

        results = await _run_requests(config, run_dir)
        metrics = aggregate_video_results(results)
        write_json(
            execution_dir,
            "raw_results.json",
            [item.to_dict() for item in results],
        )
        write_json(execution_dir, "metrics.json", metrics)
        log_video_summary(config, metrics)
        if config.outputs.includes("wandb"):
            try:
                publish_video_to_wandb(config, metrics, results, run_dir)
            except Exception as exc:
                logger.warning(
                    "Video W&B upload failed; local artifacts remain available: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        output_dir = execution_dir if config.outputs.includes("local") else None
        if output_dir is not None:
            logger.info("Video artifacts: %s", output_dir)
        return {"metrics": metrics, "output_dir": output_dir}
