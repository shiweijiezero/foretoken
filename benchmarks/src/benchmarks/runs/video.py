# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Request execution and result persistence for video benchmarks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.integrations.video import VideoGenerationClient, VideoSampleResult
from benchmarks.model_service import require_health_endpoint
from benchmarks.results.output import BenchmarkRun, ResultOutputs
from benchmarks.results.video import (
    aggregate_video_results,
    create_video_benchmark_run,
    video_result_record,
    video_result_sinks,
    video_run_record,
)

logger = logging.getLogger(__name__)


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
        return {"metrics": {"request_num": 0, "success_num": 0}, "dry_run": True, "exit_code": 0}

    record = video_run_record(config)
    result_output_dir = None
    with ResultOutputs(
        config,
        None,
        output_dir=output_dir,
        directory_prefix=f"{config.name}_",
        sink_factory=partial(video_result_sinks, config),
    ) as outputs:
        await require_health_endpoint(config.endpoint.health_url)
        logger.info("Video server health check passed")
        run_dir = Path(outputs.execution_dir)
        outputs.open(record)
        if config.warmup_requests:
            warmup_dir = run_dir / "warmup"
            warmup_dir.mkdir(parents=True, exist_ok=True)
            warmup_started = time.perf_counter()
            warmup = await _run_requests(
                config,
                warmup_dir,
                outputs,
                config.requests[: config.warmup_requests],
                warmup=True,
                duration_s=None,
                respect_config_duration=False,
            )
            outputs.record_warmup(
                [video_result_record(item, run_dir) for item in warmup],
                {**aggregate_video_results(warmup), "benchmark_time": time.perf_counter() - warmup_started},
            )
            if not warmup or any(not item.success for item in warmup):
                raise ValueError("Warmup requests failed; measurement was not started")
        run: BenchmarkRun

        def measured(results: list[VideoSampleResult]) -> None:
            """Retain formal results before the request client closes."""
            nonlocal run
            run = create_video_benchmark_run(record, results, run_dir)
            outputs.save_measurement(run)

        await _run_requests(config, run_dir, outputs, on_complete=measured)
        outputs.publish(run)
        if config.outputs.saves_local:
            result_output_dir = outputs.execution_dir
    return {"metrics": run.metrics, "output_dir": result_output_dir, "exit_code": run.exit_code}


async def _run_requests(
    config: VideoBenchmarkConfig,
    run_dir: Path,
    outputs: ResultOutputs,
    requests=None,
    *,
    warmup: bool = False,
    duration_s: float | None = None,
    respect_config_duration: bool = True,
    on_complete: Callable[[list[VideoSampleResult]], None] | None = None,
) -> list[VideoSampleResult]:
    """Execute bounded video requests, retaining each completion before live publication."""
    writer = outputs.create_request_writer("warmup_raw_output" if warmup else "raw_results")
    client = VideoGenerationClient(config)
    selected = tuple(config.requests if requests is None else requests)
    semaphore = asyncio.Semaphore(config.concurrency)
    completed = 0
    successful = 0
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
            except TimeoutError:
                return None
        else:
            await semaphore.acquire()
        try:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.sample_id).strip(
                ".-"
            )
            output = run_dir / f"video_{index:04d}_{safe_id or 'sample'}.mp4"
            result = await client.generate(request, index, output)
            # Retain completed requests before SDK publication so interruption
            # never discards their media paths, timings, or failure details.
            writer.write(video_result_record(result, Path(outputs.execution_dir)))
            return result
        finally:
            semaphore.release()

    tasks: list[asyncio.Task[VideoSampleResult | None]] = []
    try:
        ffprobe_warning = await client.prepare_video_validation()
        if ffprobe_warning is not None:
            logger.warning(
                "Generated video metadata validation is disabled: %s",
                ffprobe_warning,
            )
        if not warmup:
            outputs.start_measurement()
        tasks = [asyncio.create_task(one(index)) for index in range(len(selected))]
        results = []
        for count, pending in enumerate(asyncio.as_completed(tasks), start=1):
            result = await pending
            if result is not None:
                results.append(result)
            if count == len(tasks):
                results.sort(key=lambda item: item.index)
                if on_complete is not None:
                    on_complete(results)
            if result is None:
                continue
            completed += 1
            successful += int(result.success)
            if not warmup:
                row = {
                    "video/completed_requests": completed,
                    "video/successful_requests": successful,
                    "video/failed_requests": completed - successful,
                    "video/request/index": result.index,
                    "video/request/client_e2e_s": result.client_e2e_s,
                }
                if result.success:
                    row["video/request/e2e_s"] = result.e2e_s
                outputs.publish_progress(row, axis="video/completed_requests")
            if result.success:
                logger.info(
                    "%s request %s completed: %s completed, %s successful, %s failed; client latency %.2fs",
                    "Warmup video" if warmup else "Video", result.sample_id,
                    completed, successful, completed - successful, result.client_e2e_s,
                )
            else:
                logger.error(
                    "%s request %s failed after %.2fs: %s",
                    "Warmup video" if warmup else "Video", result.sample_id,
                    result.client_e2e_s, result.error,
                )
        return results
    finally:
        # Request tasks must release permits and streams before transport closure.
        error = sys.exception()
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await client.close()
        except BaseException:
            if error is None:
                raise
            logger.exception("Video client cleanup also failed")
