# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Video adapter for the shared parameter-sweep lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any, ClassVar

from pydantic import ValidationError

from benchmarks.config.benchmark import parse_duration_seconds
from benchmarks.config.decoding import INTEGER, NUMBER, STRING
from benchmarks.config.video import VideoBenchmarkConfig, VideoParameterSweepConfig
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.recording import wandb_run_timestamp
from benchmarks.runs.video import run_video_benchmark
from benchmarks.sweeps.core import (
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    run_sweep,
)

_VIDEO_SWEEP_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "width": ("width", INTEGER.validate_python),
    "height": ("height", INTEGER.validate_python),
    "num_frames": ("num_frames", INTEGER.validate_python),
    "fps": ("fps", INTEGER.validate_python),
    "num_inference_steps": ("num_inference_steps", INTEGER.validate_python),
    "aspect_ratio": ("aspect_ratio", STRING.validate_python),
    "flow_shift": ("flow_shift", NUMBER.validate_python),
    "audio_flow_shift": ("audio_flow_shift", NUMBER.validate_python),
    "seed": ("seed", INTEGER.validate_python),
    "max_concurrency": ("concurrency", INTEGER.validate_python),
    "duration": ("duration_s", parse_duration_seconds),
    "warmup_requests": ("warmup_requests", INTEGER.validate_python),
}


def run_video_sweep(
    config: VideoBenchmarkConfig,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run video repetitions through shared directories and scalar summaries."""
    sweep = config.sweep
    execution = run_sweep(
        config,
        SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name, sweep.resume),
        _VideoSweepAdapter(),
        mode="video_parameter_sweep",
        dry_run=dry_run,
    )
    return {
        "dry_run": dry_run,
        "metrics": execution.run.metrics,
        "exit_code": execution.run.exit_code,
        "output_dir": execution.experiment_dir if config.outputs.saves_local else None,
    }


class _VideoSweepAdapter(SweepAdapter[VideoBenchmarkConfig]):
    """Apply and execute video points without importing HTTP result semantics."""

    axis_fields: ClassVar[dict[str, Callable[[Any], Any]]] = {
        key: field[1] for key, field in _VIDEO_SWEEP_FIELDS.items()
    }

    def validate_record(self, record: SweepPoint, line_no: int) -> None:
        unknown = set(record) - set(_VIDEO_SWEEP_FIELDS) - {
            _BENCHMARK_NAME,
            _PARAMETER_GROUP,
        }
        if unknown:
            raise ValueError(
                f"Unsupported video sweep keys on line {line_no}: "
                + ", ".join(sorted(unknown))
            )
        for key in (_BENCHMARK_NAME, _PARAMETER_GROUP):
            if key in record:
                try:
                    STRING.validate_python(record[key])
                except ValidationError as error:
                    raise ValueError(f"Invalid video sweep field {key!r}: {error}") from error

    def apply_point(
        self,
        config: VideoBenchmarkConfig,
        point: SweepPoint,
    ) -> VideoBenchmarkConfig:
        """Apply video axes to each dataset request and its run schedule without mutating the base configuration."""
        updates = {
            attribute: point[key]
            for key, (attribute, _) in _VIDEO_SWEEP_FIELDS.items()
            if key in point
        }
        request_updates = {
            key: updates[key]
            for key in (
                "width",
                "height",
                "num_frames",
                "fps",
                "num_inference_steps",
                "aspect_ratio",
                "flow_shift",
                "audio_flow_shift",
                "seed",
            )
            if key in updates
        }
        requests = tuple(replace(request, **request_updates) for request in config.requests)
        return replace(
            config,
            requests=requests,
            concurrency=updates.get("concurrency", config.concurrency),
            duration_s=updates.get("duration_s", config.duration_s),
            warmup_requests=updates.get("warmup_requests", config.warmup_requests),
        )

    def validate_point(self, config: VideoBenchmarkConfig) -> None:
        config.validate()

    def plan_base(self, config: VideoBenchmarkConfig) -> dict[str, Any]:
        return config.to_dict()

    def group_name(self, config: VideoBenchmarkConfig) -> str:
        return config.wandb.group.strip() or f"video_{wandb_run_timestamp()}"

    def execute_point(
        self,
        config: VideoBenchmarkConfig,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> BenchmarkRun:
        """Run one video repetition through the existing video runner and return its measured outcome."""
        run_config = replace(
            config,
            sweep=VideoParameterSweepConfig(),
            wandb=replace(config.wandb, group=wandb_group, run_name=label),
        )
        result = asyncio.run(run_video_benchmark(
            run_config,
            dry_run=dry_run,
            output_dir=output_dir,
        ))
        return BenchmarkRun(run_config.to_dict(), result["metrics"], None, {}, exit_code=result["exit_code"])
