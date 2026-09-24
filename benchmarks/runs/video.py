# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Request execution and result persistence for video benchmarks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from functools import partial
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoGenerationClient, VideoSampleResult
from benchmarks.model_service import require_health_endpoint
from benchmarks.results.output import ResultOutputs
from benchmarks.results.video import (
    create_video_benchmark_run,
    video_result_sinks,
    video_run_record,
)

logger = logging.getLogger(__name__)


async def _run_requests(
    config: VideoBenchmarkConfig,
    run_dir: Path,
    requests=None,
    *,
    duration_s: float | None = None,
    respect_config_duration: bool = True,
) -> list[VideoSampleResult]:
    """Send selected dataset rows with the configured concurrency limit."""
    client = VideoGenerationClient(config)
    selected = tuple(config.requests if requests is None else requests)
    semaphore = asyncio.Semaphore(config.concurrency)
    started = time.perf_counter()
    admission_duration = (
        config.duration_s if duration_s is None and respect_config_duration else duration_s
    )

    async def one(index: int) -> VideoSampleResult | None:
        request = selected[index]
        if admission_duration is not None:
            remaining = admission_duration - (time.perf_counter() - started)
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(semaphore.acquire(), remaining)
            except asyncio.TimeoutError:
                return None
        else:
            await semaphore.acquire()
        try:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.sample_id).strip(
                ".-"
            )
            output = run_dir / f"video_{index:04d}_{safe_id or 'sample'}.mp4"
            return await client.generate(request, index, output)
        finally:
            semaphore.release()

    try:
        ffprobe_warning = await client.prepare_video_validation()
        if ffprobe_warning is not None:
            logger.warning(
                "Generated video metadata validation is disabled: %s",
                ffprobe_warning,
            )
        results = await asyncio.gather(
            *(one(index) for index in range(len(selected)))
        )
        return [item for item in results if item is not None]
    finally:
        await client.close()


async def run_video_benchmark(
    config: VideoBenchmarkConfig,
    *,
    dry_run: bool = False,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Send video requests and persist generated media, samples, and metrics."""
    if dry_run:
        logger.info(
            "Video dry run validated config:\n%s",
            json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        )
        return {"metrics": {"request_num": 0, "success_num": 0}, "dry_run": True}

    await require_health_endpoint(config.endpoint.health_url)
    logger.info("Video server health check passed")

    record = video_run_record(config)
    result_output_dir = None
    with ResultOutputs(
        config,
        None,
        record,
        output_dir=output_dir,
        directory_prefix=f"{config.name}_",
        sink_factory=partial(video_result_sinks, config),
    ) as outputs:
        run_dir = Path(outputs.execution_dir)
        if config.warmup_requests:
            warmup_dir = run_dir / "warmup"
            warmup_dir.mkdir(parents=True, exist_ok=True)
            warmup = await _run_requests(
                config,
                warmup_dir,
                config.requests[: config.warmup_requests],
                duration_s=None,
                respect_config_duration=False,
            )
            if not warmup or any(not item.success for item in warmup):
                raise ValueError("Warmup requests failed; measurement was not started")
        results = await _run_requests(config, run_dir)
        run = create_video_benchmark_run(record, results, run_dir)
        outputs.publish(run)
        if config.outputs.includes("local"):
            result_output_dir = outputs.execution_dir
    return {"metrics": run.metrics, "output_dir": result_output_dir}
