"""CLI option definitions for ``foretoken bench``.

Validates flags → ``BenchArguments``, then hands off to ``run_benchmark``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from benchmarks.arguments import BenchArguments, cli_default
from benchmarks.main import run_benchmark

Option = typer.Option
_d = cli_default  # Option defaults ← BenchArguments (single source)


def build_bench_arguments(**opts: Any) -> BenchArguments:
    """Validate CLI opts and build ``BenchArguments``."""
    if not (opts.get("url") and opts.get("model")):
        raise typer.BadParameter(
            "--url and --model are required for `foretoken bench`"
        )
    return BenchArguments.from_cli(**opts)


def bench(
    # --- target ---
    url: str = Option(..., help="OpenAI compatible API URL"),
    model: str = Option(..., help="Model name"),
    api_key: str = Option(_d("api_key"), help="API key (optional)"),
    timeout: int = Option(_d("timeout"), help="Request timeout seconds"),
    # --- load shape ---
    parallel: str = Option(
        _d("parallel"), help="Concurrency; list like 1,2,4,8 triggers sweep"
    ),
    number: str = Option(
        _d("number"), help="Request count; list aligns with parallel"
    ),
    rate: str = Option(
        _d("rate"),
        help=(
            "Arrival rate (req/s). -1 = no pacing; >0 = Poisson pacing "
            "(EvalScope-style absolute time). Still closed-loop (semaphore) "
            "unless --open-loop. List e.g. 5,10,20 for sweep"
        ),
    ),
    open_loop: bool = Option(
        _d("open_loop"),
        "--open-loop/--closed-loop",
        help=(
            "Open-loop: no concurrency limit (EvalScope parallel=-1); "
            "optionally pace with --rate. Default is closed-loop "
            "(semaphore = --parallel)."
        ),
    ),
    # --- generation ---
    max_tokens: int = Option(_d("max_tokens"), help="Max generation tokens"),
    temperature: float = Option(_d("temperature"), help="Sampling temperature"),
    stream: bool = Option(
        _d("stream"), help="Use streaming to measure TTFT/TPOT"
    ),
    # --- dataset / prompts ---
    dataset: str = Option(
        _d("dataset"),
        help=(
            "Dataset mode: EvalScope plugin (openqa | random | "
            "line_by_line | custom_multi_turn | ...) or multi-dataset "
            "schedule (sequential | mixed) with multiple --dataset-path. "
            "Not a file path."
        ),
    ),
    dataset_path: str = Option(
        _d("dataset_path"),
        help=(
            "Dataset file or directory path(s), comma-separated. With "
            "--dataset sequential|mixed use multiple paths (Phase 3). "
            "Example: conversation.jsonl with --dataset custom_multi_turn"
        ),
    ),
    dataset_offset: int = Option(
        _d("dataset_offset"),
        help="Global token-sequence offset for random datasets",
    ),
    tokenizer_path: str = Option(
        _d("tokenizer_path"),
        help="Tokenizer path (required for --dataset random)",
    ),
    min_prompt_length: int = Option(
        _d("min_prompt_length"),
        help="Minimum prompt length (tokens if tokenizer set)",
    ),
    max_prompt_length: int = Option(
        _d("max_prompt_length"),
        help="Maximum prompt length (tokens if tokenizer set)",
    ),
    prefix_length: int = Option(
        _d("prefix_length"), help="Prefix length (random dataset only)"
    ),
    apply_chat_template: bool | None = Option(
        _d("apply_chat_template"),
        "--apply-chat-template/--no-apply-chat-template",
        help="Apply chat template (default: auto from URL)",
    ),
    prompt: str = Option(
        _d("prompt"),
        help="Fixed prompt text (evalscope --prompt; overrides dataset)",
    ),
    max_turns: int | None = Option(
        _d("max_turns"),
        help="Max user turns for custom_multi_turn (truncate long chats)",
    ),
    # --- analysis / outputs ---
    sla_auto_tune: bool = Option(
        _d("sla_auto_tune"), help="Enable SLA search (Phase 2 — not implemented)"
    ),
    gpu_count: int = Option(
        _d("gpu_count"), help="GPU count for Pareto tokens/s/GPU axis"
    ),
    eval_suite: str = Option(
        _d("eval_suite"),
        help="none | general | tool | both (Phase 0.5 — not implemented)",
    ),
    outputs_dir: str = Option(
        _d("outputs_dir"), help="Results root directory"
    ),
    # --- wandb ---
    wandb: bool = Option(
        _d("wandb"), help="Enable Weights & Biases logging"
    ),
    wandb_project: str = Option(_d("wandb_project"), help="W&B project"),
    wandb_entity: str = Option(_d("wandb_entity"), help="W&B entity"),
    wandb_run_name: str = Option(
        _d("wandb_run_name"),
        help=(
            "W&B run name; default {model}_{YYYYMMDD_HHMMSS} "
            "(EvalScope style)"
        ),
    ),
    # --- engine metrics ---
    collect_engine_metrics: bool = Option(
        _d("collect_engine_metrics"),
        help="Collect vLLM engine metrics (Phase M1)",
    ),
    engine_metrics_url: str = Option(
        _d("engine_metrics_url"), help="Prometheus /metrics URL"
    ),
    engine_metrics_interval: float = Option(
        _d("engine_metrics_interval"),
        help="Engine /metrics polling interval seconds",
    ),
    # --- param sweep (serve × bench product) ---
    serve_params: str = Option(
        _d("serve_params"),
        help=(
            "JSON path of serve parameter combinations (list of dicts or "
            "named dict; vLLM-compatible). Cartesian product with "
            "--bench-params; recorded as metadata only (external server "
            "must already match)."
        ),
    ),
    bench_params: str = Option(
        _d("bench_params"),
        help=(
            "JSON path of bench parameter combinations (list of dicts or "
            "named dict; vLLM-compatible). Keys override foretoken bench "
            "flags (parallel/max_concurrency, number/num_prompts, ...)."
        ),
    ),
    link_vars: str = Option(
        _d("link_vars"),
        help=(
            "Comma-separated serve_key=bench_key filters for the product, "
            "e.g. max_num_seqs=parallel"
        ),
    ),
    num_runs: int = Option(
        _d("num_runs"),
        help="Repeats per serve×bench combination (param sweep)",
    ),
    dry_run: bool = Option(
        _d("dry_run"),
        "--dry-run",
        help="Print serve×bench plan without executing",
    ),
    experiment_name: str = Option(
        _d("experiment_name"),
        help="Param-sweep experiment subdir under --outputs-dir",
    ),
) -> None:
    """``foretoken bench [OPTIONS]`` — validate flags, then run."""
    args = build_bench_arguments(**locals())
    path = f" path={args.primary_dataset_path}" if args.dataset_path else ""
    print(
        "\n==============================\n"
        " Foretoken Benchmark\n"
        "==============================\n"
        f"Configuration:\n"
        f"  URL        : {args.url}\n"
        f"  Model      : {args.model}\n"
        f"  Parallel   : {args.parallel}\n"
        f"  Number     : {args.number}\n"
        f"  Rate       : {args.rate}\n"
        f"  Open Loop  : {args.open_loop}\n"
        f"  Stream     : {args.stream}\n"
        f"  Dataset    : mode={args.dataset}{path}\n"
    )
    asyncio.run(run_benchmark(args))
