"""CLI options, parsing, and command handlers for ``foretoken bench``."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import typer

from benchmarks.arguments import BenchArguments, cli_default
from benchmarks.main import run_benchmark

Option = typer.Option
_d = cli_default  # Option defaults ← BenchArguments (single source)


def run_bench(args: BenchArguments) -> None:
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


def build_bench_arguments(
    *,
    ctx: Optional[typer.Context] = None,
    require_url_model: bool = True,
    **opts: Any,
) -> Optional[BenchArguments]:
    """Validate CLI opts and build ``BenchArguments``.

    Returns ``None`` when a Typer subcommand was invoked (callback should no-op).
    """
    if ctx is not None and ctx.invoked_subcommand is not None:
        return None

    dry_run = bool(opts.get("dry_run", False))
    param_sweep = bool(opts.get("serve_params") or opts.get("bench_params"))
    if dry_run and param_sweep:
        opts["url"] = opts.get("url") or "http://127.0.0.1:8000/v1/chat/completions"
        opts["model"] = opts.get("model") or "model"
    elif require_url_model and not (opts.get("url") and opts.get("model")):
        raise typer.BadParameter(
            "--url and --model are required for `foretoken bench` "
            "(except --dry-run param sweep)"
        )
    return BenchArguments.from_cli(**opts)


def _dispatch(ctx: Optional[typer.Context] = None, **opts: Any) -> None:
    args = build_bench_arguments(ctx=ctx, **opts)
    if args is not None:
        run_bench(args)


def bench(
    ctx: typer.Context,
    url: Optional[str] = Option(None, help="OpenAI compatible API URL"),
    model: Optional[str] = Option(None, help="Model name"),
    api_key: str = Option(_d("api_key"), help="API key (optional)"),
    timeout: int = Option(_d("timeout"), help="Request timeout seconds"),
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
    max_tokens: int = Option(_d("max_tokens"), help="Max generation tokens"),
    temperature: float = Option(_d("temperature"), help="Sampling temperature"),
    stream: bool = Option(
        _d("stream"), help="Use streaming to measure TTFT/TPOT"
    ),
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
    apply_chat_template: Optional[bool] = Option(
        _d("apply_chat_template"),
        "--apply-chat-template/--no-apply-chat-template",
        help="Apply chat template (default: auto from URL)",
    ),
    prompt: str = Option(
        _d("prompt"),
        help="Fixed prompt text (evalscope --prompt; overrides dataset)",
    ),
    max_turns: Optional[int] = Option(
        _d("max_turns"),
        help="Max user turns for custom_multi_turn (truncate long chats)",
    ),
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
    """Unified entry: `foretoken bench [OPTIONS]`."""
    _dispatch(ctx, **{k: v for k, v in locals().items() if k != "ctx"})


def run_legacy(
    url: str = Option(..., help="OpenAI compatible API URL"),
    model: str = Option(..., help="Model name"),
    number: int = Option(
        100, "--number", "--num-prompts",
        help="Number of requests (alias: --num-prompts)",
    ),
    parallel: int = Option(
        1, "--parallel", "--concurrency",
        help="Max parallel requests (alias: --concurrency); default 1",
    ),
    max_tokens: int = Option(_d("max_tokens")),
    temperature: float = Option(_d("temperature")),
    stream: bool = Option(_d("stream")),
    dataset_path: str = Option(_d("dataset_path")),
    outputs_dir: str = Option(_d("outputs_dir")),
    api_key: str = Option(_d("api_key")),
    timeout: int = Option(_d("timeout")),
) -> None:
    """Deprecated thin wrapper → unified bench (prefer `foretoken bench`)."""
    _dispatch(
        **{**locals(), "parallel": str(parallel), "number": str(number)}
    )


def sweep_legacy(
    url: str = Option(...),
    model: str = Option(...),
    number: int = Option(100, "--number", "--num-prompts"),
    parallel: str = Option(
        "1,2,4,8,16",
        "--parallel",
        "--concurrency-list",
        help="Comma-separated parallel values (alias: --concurrency-list)",
    ),
    max_tokens: int = Option(_d("max_tokens")),
    temperature: float = Option(_d("temperature")),
    stream: bool = Option(_d("stream")),
    dataset_path: str = Option(_d("dataset_path")),
    outputs_dir: str = Option(_d("outputs_dir")),
    api_key: str = Option(_d("api_key")),
    timeout: int = Option(_d("timeout")),
) -> None:
    """Deprecated thin wrapper → unified bench sweep."""
    _dispatch(**{**locals(), "number": str(number)})
