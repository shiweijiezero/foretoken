# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""CLI argument definition and parsing → ``BenchConfig``."""

from __future__ import annotations

import argparse
from dataclasses import MISSING, fields
from typing import Any, Sequence

from benchmarks.config import (
    BenchConfig,
    DatasetConfig,
    EngineMetricsConfig,
    GenerationConfig,
    LoadConfig,
    OutputConfig,
    ParamSweepConfig,
    TargetConfig,
    WandbConfig,
)


def _default(cls: type, name: str) -> Any:
    f = next(x for x in fields(cls) if x.name == name)
    if f.default_factory is not MISSING:
        return f.default_factory()
    if f.default is not MISSING:
        return f.default
    raise KeyError(name)


def parse_arguments(argv: Sequence[str] | None = None) -> BenchConfig:
    """Parse ``foretoken bench`` CLI into ``BenchConfig``."""
    parser = argparse.ArgumentParser(
        prog="foretoken",
        description=(
            "LLM inference tool"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bench = subparsers.add_parser(
        "bench",
        help="Run an inference benchmark",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Target
    bench.add_argument("--url", required=True, help="OpenAI compatible API URL")
    bench.add_argument("--model", required=True, help="Model name")
    bench.add_argument(
        "--api-key", default=_default(TargetConfig, "api_key"), help="API key"
    )
    bench.add_argument(
        "--timeout",
        type=int,
        default=_default(TargetConfig, "timeout"),
        help="Request timeout seconds",
    )

    # Load
    bench.add_argument(
        "--parallel",
        type=lambda s: [int(x.strip()) for x in s.split(",") if x.strip()],
        default=_default(LoadConfig, "parallel"),
        help="Concurrency; list like 1,2,4,8 triggers sweep",
    )
    bench.add_argument(
        "--number",
        type=lambda s: [int(x.strip()) for x in s.split(",") if x.strip()],
        default=_default(LoadConfig, "number"),
        help="Request count; list aligns with parallel",
    )
    bench.add_argument(
        "--rate",
        type=lambda s: [float(x.strip()) for x in s.split(",") if x.strip()],
        default=_default(LoadConfig, "rate"),
        help=(
            "Arrival rate (req/s). -1 = no pacing; >0 = Poisson pacing. "
            "Still closed-loop unless --open-loop. List e.g. 5,10,20 for sweep"
        ),
    )
    bench.add_argument(
        "--open-loop",
        action="store_true",
        default=_default(LoadConfig, "open_loop"),
        help=(
            "Open-loop: no concurrency limit (EvalScope parallel=-1); "
            "optionally pace with --rate"
        ),
    )

    # Generation
    bench.add_argument(
        "--max-tokens",
        type=int,
        default=_default(GenerationConfig, "max_tokens"),
        help="Max generation tokens",
    )
    bench.add_argument(
        "--temperature",
        type=float,
        default=_default(GenerationConfig, "temperature"),
        help="Sampling temperature",
    )
    bench.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=_default(GenerationConfig, "stream"),
        help="Use streaming to measure TTFT/TPOT",
    )

    # Dataset
    bench.add_argument(
        "--dataset",
        default=_default(DatasetConfig, "dataset"),
        help=(
            "Dataset mode: EvalScope plugin or sequential|mixed with "
            "multiple --dataset-path. Not a file path."
        ),
    )
    bench.add_argument(
        "--dataset-path",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=_default(DatasetConfig, "dataset_path"),
        help="Dataset path(s), comma-separated",
    )
    bench.add_argument(
        "--dataset-offset",
        type=int,
        default=_default(DatasetConfig, "dataset_offset"),
        help="Global token-sequence offset for random datasets",
    )
    bench.add_argument(
        "--tokenizer-path",
        default=_default(DatasetConfig, "tokenizer_path"),
        help="Tokenizer path (required for --dataset random)",
    )
    bench.add_argument(
        "--min-prompt-length",
        type=int,
        default=_default(DatasetConfig, "min_prompt_length"),
        help="Minimum prompt length",
    )
    bench.add_argument(
        "--max-prompt-length",
        type=int,
        default=_default(DatasetConfig, "max_prompt_length"),
        help="Maximum prompt length",
    )
    bench.add_argument(
        "--prefix-length",
        type=int,
        default=_default(DatasetConfig, "prefix_length"),
        help="Prefix length (random dataset only)",
    )
    bench.add_argument(
        "--apply-chat-template",
        action=argparse.BooleanOptionalAction,
        default=_default(DatasetConfig, "apply_chat_template"),
        help="Apply chat template (default: auto from URL)",
    )
    bench.add_argument(
        "--prompt",
        default=_default(DatasetConfig, "prompt"),
        help="Fixed prompt text; overrides dataset",
    )
    bench.add_argument(
        "--max-turns",
        type=int,
        default=_default(DatasetConfig, "max_turns"),
        help="Max user turns for custom_multi_turn",
    )

    # Output
    bench.add_argument(
        "--sla-auto-tune",
        action=argparse.BooleanOptionalAction,
        default=_default(OutputConfig, "sla_auto_tune"),
        help="Enable SLA search (Phase 2 — not implemented)",
    )
    bench.add_argument(
        "--gpu-count",
        type=int,
        default=_default(OutputConfig, "gpu_count"),
        help="GPU count for Pareto tokens/s/GPU axis",
    )
    bench.add_argument(
        "--eval-suite",
        default=_default(OutputConfig, "eval_suite"),
        help="none | general | tool | both (Phase 0.5 — not implemented)",
    )
    bench.add_argument(
        "--outputs-dir",
        default=_default(OutputConfig, "outputs_dir"),
        help="Results root directory",
    )

    # WandB
    bench.add_argument(
        "--wandb",
        action=argparse.BooleanOptionalAction,
        default=_default(WandbConfig, "enabled"),
        help="Enable Weights & Biases logging",
    )
    bench.add_argument(
        "--wandb-project",
        default=_default(WandbConfig, "project"),
        help="W&B project",
    )
    bench.add_argument(
        "--wandb-entity",
        default=_default(WandbConfig, "entity"),
        help="W&B entity",
    )
    bench.add_argument(
        "--wandb-run-name",
        default=_default(WandbConfig, "run_name"),
        help="W&B run name; default {model}_{YYYYMMDD_HHMMSS}",
    )

    # Engine metrics
    bench.add_argument(
        "--collect-engine-metrics",
        action=argparse.BooleanOptionalAction,
        default=_default(EngineMetricsConfig, "collect"),
        help="Collect vLLM engine metrics (Phase M1)",
    )
    bench.add_argument(
        "--engine-metrics-url",
        default=_default(EngineMetricsConfig, "url"),
        help="Prometheus /metrics URL",
    )
    bench.add_argument(
        "--engine-metrics-interval",
        type=float,
        default=_default(EngineMetricsConfig, "interval"),
        help="Engine /metrics polling interval seconds",
    )

    # Param sweep
    bench.add_argument(
        "--serve-params",
        default=_default(ParamSweepConfig, "serve_params"),
        help="JSON path of serve parameter combinations",
    )
    bench.add_argument(
        "--bench-params",
        default=_default(ParamSweepConfig, "bench_params"),
        help="JSON path of bench parameter combinations",
    )
    bench.add_argument(
        "--link-vars",
        default=_default(ParamSweepConfig, "link_vars"),
        help="Comma-separated serve_key=bench_key product filters",
    )
    bench.add_argument(
        "--num-runs",
        type=int,
        default=_default(ParamSweepConfig, "num_runs"),
        help="Repeats per serve×bench combination",
    )
    bench.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=_default(ParamSweepConfig, "dry_run"),
        help="Print serve×bench plan without executing",
    )
    bench.add_argument(
        "--experiment-name",
        default=_default(ParamSweepConfig, "experiment_name"),
        help="Param-sweep experiment subdir under --outputs-dir",
    )

    ns = parser.parse_args(argv)
    return BenchConfig(
        target=TargetConfig(
            url=ns.url, model=ns.model, api_key=ns.api_key, timeout=ns.timeout
        ),
        load=LoadConfig(
            parallel=ns.parallel,
            number=ns.number,
            rate=ns.rate,
            open_loop=ns.open_loop,
        ),
        generation=GenerationConfig(
            max_tokens=ns.max_tokens,
            temperature=ns.temperature,
            stream=ns.stream,
        ),
        dataset=DatasetConfig(
            dataset=ns.dataset,
            dataset_path=ns.dataset_path,
            dataset_offset=ns.dataset_offset,
            tokenizer_path=ns.tokenizer_path,
            min_prompt_length=ns.min_prompt_length,
            max_prompt_length=ns.max_prompt_length,
            prefix_length=ns.prefix_length,
            apply_chat_template=ns.apply_chat_template,
            prompt=ns.prompt,
            max_turns=ns.max_turns,
        ),
        output=OutputConfig(
            outputs_dir=ns.outputs_dir,
            gpu_count=ns.gpu_count,
            eval_suite=ns.eval_suite,
            sla_auto_tune=ns.sla_auto_tune,
        ),
        wandb=WandbConfig(
            enabled=ns.wandb,
            project=ns.wandb_project,
            entity=ns.wandb_entity,
            run_name=ns.wandb_run_name,
        ),
        engine=EngineMetricsConfig(
            collect=ns.collect_engine_metrics,
            url=ns.engine_metrics_url,
            interval=ns.engine_metrics_interval,
        ),
        param_sweep=ParamSweepConfig(
            serve_params=ns.serve_params,
            bench_params=ns.bench_params,
            link_vars=ns.link_vars,
            num_runs=ns.num_runs,
            dry_run=ns.dry_run,
            experiment_name=ns.experiment_name,
        ),
    )
