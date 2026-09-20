# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""CLI mapping for request-only video-generation benchmarks."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from benchmarks.config.benchmark import BenchmarkOutputConfig, WandbRunConfig
from benchmarks.config.cli import _add_benchmark_arguments
from benchmarks.config.video import (
    VideoBenchmarkConfig,
    VideoDatasetDefaults,
    VideoEndpointConfig,
    video_health_url,
)
from benchmarks.datasets.video import (
    load_video_dataset,
    video_dataset_name,
)


@dataclass(frozen=True)
class VideoBenchCommand:
    """Run a video-generation benchmark against an existing endpoint."""

    config: VideoBenchmarkConfig
    dry_run: bool = False


def parse_video_arguments(
    argv: Sequence[str], *, command_name: str = "video"
) -> VideoBenchCommand:
    """Map the video CLI surface to its request and result configuration."""
    parser = argparse.ArgumentParser(
        prog=f"foretoken bench {command_name}",
        description="Benchmark an existing synchronous video-generation endpoint",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_benchmark_arguments(parser, video=True)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--num-frames", type=int, default=124)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--num-inference-steps", type=int, default=50)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--flow-shift", type=float, default=12.0)
    parser.add_argument("--audio-flow-shift", type=float, default=3.0)
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        help="Base seed for public benchmark rows; row index is added",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the workload without sending requests",
    )
    parsed = parser.parse_args(list(argv))
    dataset_path, requests = load_video_dataset(
        parsed.dataset,
        defaults=VideoDatasetDefaults(
            width=parsed.width,
            height=parsed.height,
            num_frames=parsed.num_frames,
            fps=parsed.fps,
            num_inference_steps=parsed.num_inference_steps,
            aspect_ratio=parsed.aspect_ratio,
            flow_shift=parsed.flow_shift,
            audio_flow_shift=parsed.audio_flow_shift,
            seed=parsed.seed,
        ),
        number=parsed.number,
        offset=parsed.dataset_offset,
    )
    endpoint_url = parsed.url.rstrip("/")
    config = VideoBenchmarkConfig(
        name=video_dataset_name(dataset_path),
        dataset_source=parsed.dataset,
        dataset_path=dataset_path,
        endpoint=VideoEndpointConfig(
            url=endpoint_url,
            health_url=parsed.health_url or video_health_url(endpoint_url),
            timeout_s=parsed.timeout,
        ),
        requests=requests,
        outputs=BenchmarkOutputConfig(
            destinations=parsed.output,
            output_dir=parsed.output_dir,
        ),
        wandb=WandbRunConfig(
            project=parsed.wandb_project,
            entity=parsed.wandb_entity,
            run_name=parsed.wandb_run_name,
        ),
        dataset_offset=parsed.dataset_offset,
        concurrency=parsed.parallel,
    )
    config.validate()
    return VideoBenchCommand(config=config, dry_run=parsed.dry_run)
