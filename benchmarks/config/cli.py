# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse the ``foretoken bench`` command and build the benchmark configuration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import MISSING, fields
from typing import Any

from benchmarks.config.benchmark import (
    ArrivalTraceSchedule,
    BenchmarkConfig,
    BenchmarkOutputConfig,
    ChatCompletionsGeneration,
    ChatRequestDataset,
    HttpLoadSchedule,
    ModelServiceSource,
    ParameterSweepConfig,
    WandbRunConfig,
)


def _default(cls: type, name: str) -> Any:
    field_info = next(item for item in fields(cls) if item.name == name)
    if field_info.default_factory is not MISSING:
        return field_info.default_factory()
    if field_info.default is not MISSING:
        return field_info.default
    raise KeyError(name)


def _output_destinations(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _json_object(value: str) -> dict[str, Any]:
    """Accept only a JSON object so other JSON values cannot override request-body configuration."""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _dataset_selectors(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _add_benchmark_arguments(parser: argparse.ArgumentParser) -> None:
    # Service source
    parser.add_argument(
        "kustomize_path",
        nargs="?",
        metavar="PATH",
        help="Kustomize directory to deploy or reuse",
    )
    parser.add_argument(
        "--url",
        default=_default(ModelServiceSource, "url"),
        help="Model service URL, including /v1/chat/completions",
    )
    parser.add_argument(
        "--model",
        default=_default(ModelServiceSource, "model"),
        help="Model name; inferred when the deployment contains one model",
    )
    parser.add_argument(
        "--api-key",
        default=_default(ModelServiceSource, "api_key"),
        help="API key",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_default(ModelServiceSource, "timeout_seconds"),
        help="Request timeout seconds",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=_default(ModelServiceSource, "max_retries"),
        help="Additional attempts for transient request failures; 0 disables retries",
    )
    parser.add_argument(
        "--wait-timeout",
        default=_default(ModelServiceSource, "wait_timeout"),
        help="Timeout for each deployment readiness stage",
    )

    # HTTP workload
    parser.add_argument(
        "--parallel",
        type=int,
        default=_default(HttpLoadSchedule, "max_concurrency"),
        help=(
            "Maximum concurrent conversations; a fixed or random prompt is one "
            "turn; ignored with --open-loop"
        ),
    )
    parser.add_argument(
        "--number",
        type=int,
        default=_default(HttpLoadSchedule, "request_count"),
        help=(
            "Conversations per run; total across multiple datasets"
        ),
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=_default(HttpLoadSchedule, "arrival_rate"),
        help=(
            "Arrival rate (req/s): -1 sends as fast as possible; "
            ">0 uses Poisson arrivals"
        ),
    )
    parser.add_argument(
        "--open-loop",
        action="store_true",
        default=_default(HttpLoadSchedule, "unbounded_concurrency"),
        help="Schedule single-turn requests without a concurrency cap; requires --rate > 0",
    )

    # Chat Completions generation parameters
    parser.add_argument(
        "--max-tokens",
        type=int,
        nargs="+",
        default=_default(ChatCompletionsGeneration, "max_tokens"),
        help=(
            "Max generation tokens: one value (fixed) or two values "
            "MIN MAX for uniform sampling per request"
        ),
    )
    sampling = parser.add_argument_group("sampling parameters")
    sampling.add_argument(
        "--top-p",
        type=float,
        default=_default(ChatCompletionsGeneration, "top_p"),
        help="Top-p sampling parameter",
    )
    sampling.add_argument(
        "--top-k",
        type=int,
        default=_default(ChatCompletionsGeneration, "top_k"),
        help="Top-k sampling parameter",
    )
    sampling.add_argument(
        "--min-p",
        type=float,
        default=_default(ChatCompletionsGeneration, "min_p"),
        help="Min-p sampling parameter",
    )
    sampling.add_argument(
        "--temperature",
        type=float,
        default=_default(ChatCompletionsGeneration, "temperature"),
        help="Temperature sampling parameter",
    )
    sampling.add_argument(
        "--frequency-penalty",
        type=float,
        default=_default(ChatCompletionsGeneration, "frequency_penalty"),
        help="Frequency penalty sampling parameter",
    )
    sampling.add_argument(
        "--presence-penalty",
        type=float,
        default=_default(ChatCompletionsGeneration, "presence_penalty"),
        help="Presence penalty sampling parameter",
    )
    sampling.add_argument(
        "--repetition-penalty",
        type=float,
        default=_default(ChatCompletionsGeneration, "repetition_penalty"),
        help="Repetition penalty sampling parameter",
    )
    parser.add_argument(
        "--extra-body",
        type=_json_object,
        default=_default(ChatCompletionsGeneration, "extra_body"),
        help="JSON object of extra body parameters included in each request",
    )
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=_default(ChatCompletionsGeneration, "stream"),
        help=(
            "Stream responses (default). --no-stream sends non-streaming "
            "requests and reports latency only (no TTFT/TPOT)"
        ),
    )

    # Independent request content and arrival traces
    parser.add_argument(
        "--dataset",
        type=_dataset_selectors,
        default=_default(ChatRequestDataset, "dataset_selectors"),
        help=(
            "Comma-separated dataset selectors: random, JSONL path, Hugging Face "
            "org/name:split, or hf://datasets/...; --number is shared"
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=_default(ChatRequestDataset, "max_turns"),
        help=(
            "Maximum user turns per conversation; -1 runs the complete "
            "conversation, positive values keep the first N turns"
        ),
    )
    parser.add_argument(
        "--trace",
        dest="trace_path",
        default=_default(ArrivalTraceSchedule, "trace_selector"),
        help=(
            "Trace source: JSONL path, supported trace dataset, or "
            "hf://datasets/...; requires --dataset"
        ),
    )
    parser.add_argument(
        "--trace-start",
        type=float,
        default=_default(ArrivalTraceSchedule, "start_offset_seconds"),
        help="Start offset from the first trace timestamp, in seconds",
    )
    parser.add_argument(
        "--trace-duration",
        type=float,
        default=_default(ArrivalTraceSchedule, "duration_seconds"),
        help="Trace window duration in seconds; omit to replay to the end",
    )
    parser.add_argument(
        "--trace-max-concurrency",
        type=int,
        default=_default(ArrivalTraceSchedule, "max_concurrency"),
        help=(
            "Optional cap on active trace requests; timestamps still control "
            "arrival times"
        ),
    )
    parser.add_argument(
        "--trace-synthetic-prefix-reuse",
        action="store_true",
        default=_default(ArrivalTraceSchedule, "synthetic_prefix_reuse"),
        help=(
            "For Mooncake + random, synthesize deterministic 512-token "
            "prefix blocks from trace hash_ids"
        ),
    )
    parser.add_argument(
        "--dataset-offset",
        type=int,
        default=_default(ChatRequestDataset, "row_offset"),
        help="Skip first N samples (JSONL/HF) or token-sequence offset (random)",
    )
    parser.add_argument(
        "--tokenizer-path",
        default=_default(ChatRequestDataset, "tokenizer"),
        help="Tokenizer path (required for --dataset random)",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=_default(ChatRequestDataset, "random_seed"),
        help="Random payload seed",
    )
    parser.add_argument(
        "--min-prompt-length",
        type=int,
        default=_default(ChatRequestDataset, "minimum_prompt_tokens"),
        help="Minimum prompt length in tokens (random: sampled inner length)",
    )
    parser.add_argument(
        "--max-prompt-length",
        type=int,
        default=_default(ChatRequestDataset, "maximum_prompt_tokens"),
        help="Maximum prompt length in tokens (random: sampled inner length)",
    )
    parser.add_argument(
        "--apply-chat-template",
        action=argparse.BooleanOptionalAction,
        default=_default(ChatRequestDataset, "apply_chat_template"),
        help="Include tokenizer chat-template overhead when sizing random prompts",
    )
    parser.add_argument(
        "--prefix-length",
        type=int,
        default=_default(ChatRequestDataset, "shared_prefix_tokens"),
        help="Shared prefix token length (random dataset only)",
    )
    parser.add_argument(
        "--prompt",
        default=_default(ChatRequestDataset, "fixed_prompt"),
        help="Fixed prompt text; overrides dataset",
    )

    # Benchmark results
    parser.add_argument(
        "--output",
        type=_output_destinations,
        default=_default(BenchmarkOutputConfig, "destinations"),
        help="Comma-separated outputs: local, wandb, and quiet",
    )
    parser.add_argument(
        "--output-dir",
        default=_default(BenchmarkOutputConfig, "output_dir"),
        help="Directory for JSON and W&B artifacts",
    )

    # W&B destinations
    parser.add_argument(
        "--wandb-project",
        default=_default(WandbRunConfig, "project"),
        help="W&B project",
    )
    parser.add_argument(
        "--wandb-entity",
        default=_default(WandbRunConfig, "entity"),
        help="W&B entity",
    )
    parser.add_argument(
        "--wandb-run-name",
        default=_default(WandbRunConfig, "run_name"),
        help=(
            "W&B run-name prefix; child runs append their label. "
            "Default: {model}_{YYYYMMDD_HHMMSS}"
        ),
    )

    # Parameter sweep
    parser.add_argument(
        "--sweep",
        metavar="PATH",
        default=_default(ParameterSweepConfig, "path"),
        help=(
            "JSONL parameter combinations; parallel, number, and rate may be lists"
        ),
    )
    parser.add_argument(
        "--num-runs",
        type=int,
        default=_default(ParameterSweepConfig, "num_runs"),
        help="Runs per parameter combination",
    )
    parser.add_argument(
        "--experiment-name",
        default=_default(ParameterSweepConfig, "experiment_name"),
        help="Sweep directory name under --output-dir",
    )


def _benchmark_config(namespace: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        service=ModelServiceSource(
            kustomize_path=namespace.kustomize_path or "",
            url=namespace.url,
            model=namespace.model,
            api_key=namespace.api_key,
            timeout_seconds=namespace.timeout,
            max_retries=namespace.max_retries,
            wait_timeout=namespace.wait_timeout,
        ),
        load=HttpLoadSchedule(
            max_concurrency=namespace.parallel,
            request_count=namespace.number,
            arrival_rate=namespace.rate,
            unbounded_concurrency=namespace.open_loop,
        ),
        generation=ChatCompletionsGeneration(
            max_tokens=namespace.max_tokens,
            stream=namespace.stream,
            top_p=namespace.top_p,
            top_k=namespace.top_k,
            min_p=namespace.min_p,
            temperature=namespace.temperature,
            frequency_penalty=namespace.frequency_penalty,
            presence_penalty=namespace.presence_penalty,
            repetition_penalty=namespace.repetition_penalty,
            extra_body=namespace.extra_body,
        ),
        workload=ChatRequestDataset(
            dataset_selectors=namespace.dataset,
            row_offset=namespace.dataset_offset,
            tokenizer=namespace.tokenizer_path,
            random_seed=namespace.random_seed,
            minimum_prompt_tokens=namespace.min_prompt_length,
            maximum_prompt_tokens=namespace.max_prompt_length,
            shared_prefix_tokens=namespace.prefix_length,
            apply_chat_template=namespace.apply_chat_template,
            fixed_prompt=namespace.prompt,
            max_turns=namespace.max_turns,
        ),
        trace=ArrivalTraceSchedule(
            trace_selector=namespace.trace_path,
            start_offset_seconds=namespace.trace_start,
            duration_seconds=namespace.trace_duration,
            max_concurrency=namespace.trace_max_concurrency,
            synthetic_prefix_reuse=namespace.trace_synthetic_prefix_reuse,
        ),
        outputs=BenchmarkOutputConfig(
            destinations=namespace.output,
            output_dir=namespace.output_dir,
        ),
        wandb=WandbRunConfig(
            project=namespace.wandb_project,
            entity=namespace.wandb_entity,
            run_name=namespace.wandb_run_name,
        ),
        sweep=ParameterSweepConfig(
            path=namespace.sweep,
            num_runs=namespace.num_runs,
            experiment_name=namespace.experiment_name,
        ),
    )


def parse_benchmark_arguments(
    argv: Sequence[str] | None = None,
) -> BenchmarkConfig:
    """Parse benchmark arguments after top-level ``foretoken bench``."""
    parser = argparse.ArgumentParser(
        prog="foretoken bench",
        description=(
            "Measure HTTP latency and throughput for Foretoken and "
            "OpenAI-compatible inference services"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_benchmark_arguments(parser)

    parsed_args = parser.parse_args(argv)
    return _benchmark_config(parsed_args)
