# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse the ``foretoken perf`` command and build the performance configuration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import MISSING, fields
from typing import Any

from foretoken.arguments import add_profile_arguments, validate_profile_arguments

from benchmarks.config.benchmark import (
    ArrivalTraceSchedule,
    BenchmarkConfig,
    BenchmarkOutputConfig,
    BenchmarkProfileConfig,
    ChatCompletionsGeneration,
    ChatRequestDataset,
    HttpLoadSchedule,
    ModelServiceSource,
    ParameterSweepConfig,
    SloTuneConfig,
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


def _add_benchmark_arguments(
    parser: argparse.ArgumentParser, *, video: bool = False
) -> None:
    """Register the shared benchmark surface and mode-specific options once."""
    # Every HTTP benchmark consumes these service, load, dataset, and output options.
    parser.add_argument(
        "--url",
        required=video,
        default=None if video else _default(ModelServiceSource, "url"),
        help="Model service request endpoint URL",
    )
    parser.add_argument(
        "--health-url",
        default=_default(ModelServiceSource, "health_url"),
        help=(
            "Health endpoint; derived from --url when omitted"
            if video
            else "Optional service health endpoint checked before the benchmark"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float if video else int,
        default=_default(ModelServiceSource, "timeout_seconds"),
        help="Request timeout seconds",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=_default(HttpLoadSchedule, "max_concurrency"),
        help=(
            "Maximum concurrent video requests"
            if video
            else "Maximum concurrent requests; -1 means no concurrency limit"
        ),
    )
    parser.add_argument(
        "--num-prompts",
        type=int,
        default=None,
        help=(
            "Number of video requests; zero uses all selected rows"
            if video
            else "Prompt/work-item budget per run"
        ),
    )
    parser.add_argument(
        "--dataset",
        type=None if video else _dataset_selectors,
        required=video,
        default=None if video else _default(ChatRequestDataset, "dataset_selectors"),
        help=(
            "Native video JSONL path or an auto-downloaded selector such as "
            "VideoArgusBench/TI2V (FORETOKEN_DATA_ROOT owns local data and "
            "the download cache when set)"
            if video
            else "Comma-separated dataset selectors: random, JSONL path, Hugging Face "
            "org/name[:split], or hf://datasets/...; --num-prompts is shared"
        ),
    )
    parser.add_argument(
        "--dataset-offset",
        type=int,
        default=_default(ChatRequestDataset, "row_offset"),
        help=(
            "Number of dataset rows to skip"
            if video
            else "Skip first N samples (JSONL/HF) or token-sequence offset (random)"
        ),
    )
    parser.add_argument(
        "--output",
        type=_output_destinations,
        default=_default(BenchmarkOutputConfig, "destinations"),
        help="Comma-separated outputs: local, wandb, and quiet",
    )
    parser.add_argument(
        "--output-dir",
        default=_default(BenchmarkOutputConfig, "output_dir"),
        help="Directory for benchmark results and artifacts",
    )
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
            "W&B run name"
            if video
            else "W&B run-name prefix; child runs append their label. "
            "Default: {model}_{YYYYMMDD_HHMMSS}"
        ),
    )
    parser.add_argument(
        "--wandb-group",
        default=_default(WandbRunConfig, "group"),
        help=(
            "W&B run group"
            if video
            else (
                "Group related runs; automatically assigned for sweeps "
                "and multiple datasets"
            )
        ),
    )
    if video:
        parser.set_defaults(
            timeout=3600.0,
            num_prompts=0,
            output=("local",),
            output_dir="results/video",
        )
        return

    # Chat Completions service and orchestration options.
    parser.add_argument(
        "kustomize_path",
        nargs="?",
        metavar="PATH",
        help="Kustomize directory to deploy or reuse",
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
        "--max-retries",
        type=int,
        default=_default(ModelServiceSource, "max_retries"),
        help="Additional attempts for transient request failures; 0 disables retries",
    )
    parser.add_argument(
        "--wait-timeout",
        default=_default(ModelServiceSource, "wait_timeout"),
        help="Timeout for each deployment readiness or profile startup/completion stage",
    )

    add_profile_arguments(parser)

    # Chat Completions workload scheduling.
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=_default(HttpLoadSchedule, "warmup_requests"),
        help="Conversations to finish before each generated run; excluded from measured results",
    )
    parser.add_argument(
        "--request-rate",
        type=float,
        default=_default(HttpLoadSchedule, "arrival_rate"),
        help="Target request arrival rate in req/s; -1 sends as fast as possible",
    )
    parser.add_argument(
        "--arrival-pattern",
        choices=("constant", "poisson", "gamma"),
        default=_default(HttpLoadSchedule, "arrival_pattern"),
        help="Arrival process for generated requests or timestamp trace replay",
    )
    parser.add_argument(
        "--burstiness",
        type=float,
        default=_default(HttpLoadSchedule, "burstiness"),
        help="Gamma arrival shape; 1 is Poisson, lower values are more bursty",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=_default(HttpLoadSchedule, "duration_seconds"),
        help="Maximum measured workload duration in seconds",
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
    parser.add_argument(
        "--min-output-length", type=int,
        default=_default(ChatCompletionsGeneration, "min_output_length"),
        help="Minimum sampled output length for random workloads; requires --max-output-length",
    )
    parser.add_argument(
        "--max-output-length", type=int,
        default=_default(ChatCompletionsGeneration, "max_output_length"),
        help="Maximum sampled output length; requires service support for min_tokens and ignore_eos",
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
            "requests without token-level timing (no TTFT/TPOT)"
        ),
    )

    # Independent request content and arrival traces
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
        "--conversation-history",
        choices=("dataset", "generated"),
        default=_default(ChatRequestDataset, "conversation_history"),
        help="Answer source for conversation history; trace replay uses its recorded requests",
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

    # Chat Completions-only parameter sweeps.
    parser.add_argument(
        "--sweep",
        metavar="PATH",
        default=_default(ParameterSweepConfig, "path"),
        help=(
            "JSONL parameter combinations; execution fields may be lists and expand cartesian"
        ),
    )
    parser.add_argument(
        "--num-runs",
        type=int,
        default=_default(ParameterSweepConfig, "num_runs"),
        help="Runs per sweep combination or repeated SLO probe",
    )
    parser.add_argument(
        "--experiment-name",
        default=_default(ParameterSweepConfig, "experiment_name"),
        help="Sweep directory name under --output-dir",
    )

    parser.add_argument(
        "--slo-params",
        type=json.loads,
        default=_default(SloTuneConfig, "params"),
        help=(
            "JSON SLO criteria that enable search; metrics in one object are ANDed, "
            "objects are searched independently"
        ),
    )
    parser.add_argument(
        "--slo-upper-bound",
        type=int,
        default=_default(SloTuneConfig, "upper_bound"),
        help="Upper bound of the SLO search variable",
    )
    parser.add_argument(
        "--slo-lower-bound",
        type=int,
        default=_default(SloTuneConfig, "lower_bound"),
        help="Lower bound of the SLO search variable",
    )


def _benchmark_config(namespace: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        service=ModelServiceSource(
            kustomize_path=namespace.kustomize_path or "",
            url=namespace.url,
            health_url=namespace.health_url,
            model=namespace.model,
            api_key=namespace.api_key,
            timeout_seconds=namespace.timeout,
            max_retries=namespace.max_retries,
            wait_timeout=namespace.wait_timeout,
        ),
        load=HttpLoadSchedule(
            max_concurrency=namespace.max_concurrency,
            request_count=(
                namespace.num_prompts
                if namespace.num_prompts is not None
                else (None if namespace.duration is not None else _default(HttpLoadSchedule, "request_count"))
            ),
            arrival_rate=namespace.request_rate,
            arrival_pattern=namespace.arrival_pattern,
            burstiness=namespace.burstiness,
            warmup_requests=namespace.warmup_requests,
            duration_seconds=namespace.duration,
        ),
        generation=ChatCompletionsGeneration(
            max_tokens=namespace.max_tokens,
            min_output_length=namespace.min_output_length,
            max_output_length=namespace.max_output_length,
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
            conversation_history=namespace.conversation_history,
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
            group=namespace.wandb_group,
            run_name=namespace.wandb_run_name,
        ),
        sweep=ParameterSweepConfig(
            path=namespace.sweep,
            num_runs=namespace.num_runs,
            experiment_name=namespace.experiment_name,
        ),
        slo=SloTuneConfig(
            params=namespace.slo_params,
            num_runs=namespace.num_runs,
            upper_bound=namespace.slo_upper_bound,
            lower_bound=namespace.slo_lower_bound,
        ),
        profile=(
            BenchmarkProfileConfig(namespace.profile_engine, namespace.profile_duration)
            if namespace.profile else None
        ),
    )


def parse_benchmark_arguments(
    argv: Sequence[str] | None = None,
) -> BenchmarkConfig:
    """Parse performance arguments after top-level ``foretoken perf``."""
    parser = argparse.ArgumentParser(
        prog="foretoken perf",
        description=(
            "Measure HTTP latency and throughput for Foretoken and "
            "OpenAI-compatible inference services"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_benchmark_arguments(parser)

    parsed_args = parser.parse_args(argv)
    validate_profile_arguments(parser, parsed_args)
    return _benchmark_config(parsed_args)
