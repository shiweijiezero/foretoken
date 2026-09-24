# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Video adapter for the shared parameter-sweep lifecycle."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from benchmarks.config.video import VideoBenchmarkConfig, VideoParameterSweepConfig
from benchmarks.results.output import wandb_run_timestamp
from benchmarks.sweeps.core import (
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    expand_sweep_point,
    load_sweep_points as load_core_sweep_points,
    run_sweep,
)
from benchmarks.runs.video import run_video_benchmark

_VIDEO_SWEEP_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "width": ("width", int),
    "height": ("height", int),
    "num_frames": ("num_frames", int),
    "fps": ("fps", int),
    "num_inference_steps": ("num_inference_steps", int),
    "aspect_ratio": ("aspect_ratio", str),
    "flow_shift": ("flow_shift", float),
    "audio_flow_shift": ("audio_flow_shift", float),
    "seed": ("seed", int),
    "max_concurrency": ("concurrency", int),
    "duration": ("duration_s", float),
    "warmup_requests": ("warmup_requests", int),
}


class _VideoSweepAdapter(SweepAdapter[VideoBenchmarkConfig]):
    """Apply and execute video points without importing HTTP result semantics."""

    axis_fields = {key: field[1] for key, field in _VIDEO_SWEEP_FIELDS.items()}

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

    def apply_point(
        self,
        config: VideoBenchmarkConfig,
        point: SweepPoint,
    ) -> VideoBenchmarkConfig:
        updates = {
            attribute: caster(point[key])
            for key, (attribute, caster) in _VIDEO_SWEEP_FIELDS.items()
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

    async def execute_point(
        self,
        config: VideoBenchmarkConfig,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        run_config = replace(
            config,
            sweep=VideoParameterSweepConfig(),
            wandb=replace(config.wandb, group=wandb_group, run_name=label),
        )
        result = await run_video_benchmark(
            run_config,
            dry_run=dry_run,
            output_dir=output_dir,
        )
        return dict(result["metrics"])


def expand_video_sweep_point(record: SweepPoint) -> list[SweepPoint]:
    """Expand video-owned fields for callers that inspect combinations directly."""
    return expand_sweep_point(record, _VideoSweepAdapter.axis_fields)


def load_video_sweep_points(path: str) -> list[SweepPoint]:
    """Load and expand a video sweep file without executing requests."""
    return load_core_sweep_points(
        SweepDefinition(path, 1, ""), _VideoSweepAdapter()
    )


def apply_video_sweep_point(
    config: VideoBenchmarkConfig,
    point: SweepPoint,
) -> VideoBenchmarkConfig:
    """Apply one video-owned point for compatibility with existing callers."""
    return _VideoSweepAdapter().apply_point(config, point)


async def run_video_sweep(
    config: VideoBenchmarkConfig,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run video repetitions through shared directories and scalar summaries."""
    sweep = config.sweep
    execution = await run_sweep(
        config,
        SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name),
        _VideoSweepAdapter(),
        mode="video_parameter_sweep",
        dry_run=dry_run,
    )
    return {
        "dry_run": dry_run,
        "metrics": {
            "request_num": sum(int(point.get("request_num", 0)) for point in execution.points),
            "success_num": sum(int(point.get("success_num", 0)) for point in execution.points),
            "failed_num": sum(
                int(point.get("request_num", 0)) - int(point.get("success_num", 0))
                for point in execution.points
            ),
        },
        "output_dir": execution.experiment_dir if config.outputs.includes("local") else None,
    }


__all__ = [
    "apply_video_sweep_point",
    "expand_video_sweep_point",
    "load_video_sweep_points",
    "run_video_sweep",
]
