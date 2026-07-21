from __future__ import annotations

from typing import Any, Optional

from benchmarks.arguments import BenchArguments
from benchmarks.client.vllm_client import VLLMClient
from benchmarks.metrics.aggregator import (
    MetricsAggregator,
    attach_user_throughput,
)
from benchmarks.metrics.engine_collector import (
    build_engine_collector,
    engine_timeseries_csv,
)
from benchmarks.report.summary import (
    print_pareto_frontier,
    print_summary,
    print_sweep_results,
)
from benchmarks.report.table import build_metrics_table
from benchmarks.runner.single_runner import SingleRunner
from benchmarks.storage.csv_writer import sweep_csv
from benchmarks.storage.result_writer import ResultWriter
from benchmarks.storage.wandb_writer import (
    WandbWriter,
    default_wandb_group_name,
    sweep_point_run_name,
)

from benchmarks.analyzer.pareto import (
    attach_pareto_metrics,
    pareto_frontier,
)
from benchmarks.analyzer.vllm_compat import vllm_pareto_available
from benchmarks.runner.sweep_runner import SweepRunner
from benchmarks.visualization.pareto_plot import plot_pareto


def _load_requests(args: BenchArguments) -> Optional[list[dict]]:
    """Load requests via EvalScope perf dataset plugins (or --prompt)."""
    if args.prompt:
        return [{"prompt": args.prompt} for _ in range(args.primary_number)]

    name = (args.dataset or "").strip()
    limit = max(args.number) if args.number else args.primary_number

    try:
        from benchmarks.workload.evalscope_loader import EvalscopeDatasetLoader

        return EvalscopeDatasetLoader(args, limit=limit).load()
    except ImportError:
        from benchmarks.workload.jsonl_loader import JsonlPromptLoader

        jsonl_path = args.primary_dataset_path
        if not jsonl_path and name.endswith(".jsonl"):
            jsonl_path = name
        if not jsonl_path:
            raise
        return JsonlPromptLoader(jsonl_path, limit=limit).load()


def _infer_api_type(url: str) -> Optional[str]:
    """Rough api type for EvalScope create_message field selection."""
    u = (url or "").lower()
    if "embedding" in u:
        return "openai_embedding"
    if "rerank" in u:
        return "openai_rerank"
    if "chat/completions" in u or "completions" in u:
        return "openai"
    return "openai"


def _make_wandb(
    args: BenchArguments,
    config: dict[str, Any],
    wandb_dir: str = "",
    *,
    run_name: str = "",
    group: str = "",
    job_type: str = "",
) -> WandbWriter:
    return WandbWriter(
        enabled=args.wandb,
        project=args.wandb_project or "foretoken-bench",
        entity=args.wandb_entity,
        run_name=run_name or args.wandb_run_name,
        config=config,
        wandb_dir=wandb_dir,
        api_type=_infer_api_type(args.url),
        group=group,
        job_type=job_type,
    )


async def _collect_engine(
    args: BenchArguments,
    run_coro,
) -> tuple[Any, Optional[dict[str, Any]]]:
    """Run coroutine while optionally sampling engine metrics."""
    if not args.collect_engine_metrics:
        return await run_coro(), None

    collector = build_engine_collector(
        args.url,
        metrics_url=args.engine_metrics_url,
        api_key=args.api_key,
        interval=args.engine_metrics_interval,
    )
    await collector.start()
    try:
        output = await run_coro()
    finally:
        engine = await collector.stop()
    return output, engine


async def run_benchmark(args: BenchArguments) -> dict[str, Any]:
    """Route by args and execute the selected benchmark mode."""
    args.validate()

    if args.eval_suite != "none":
        raise NotImplementedError(
            f"--eval-suite {args.eval_suite} is planned for Phase 0.5"
        )

    if args.sla_auto_tune:
        raise NotImplementedError(
            "--sla-auto-tune is planned for Phase 2"
        )

    if args.is_multi_dataset:
        mode = (args.dataset or "").strip().lower() or "sequential"
        raise NotImplementedError(
            f"--dataset {mode} with multiple --dataset-path "
            "(multi-dataset) is planned for Phase 3"
        )

    if args.is_param_sweep:
        from benchmarks.sweep.param_runner import run_param_sweep

        return await run_param_sweep(args)

    requests = _load_requests(args)

    if args.is_sweep:
        return await _run_sweep(args, requests)

    return await _run_single(args, requests)


async def _run_single(
    args: BenchArguments,
    requests: Optional[list[dict]],
) -> dict[str, Any]:
    writer = ResultWriter(root_dir=args.outputs_dir)
    config = args.run_config()
    config["mode"] = "single"
    # Open-loop: resolved parallel matches EvalScope / metrics (-1).
    resolved_parallel = -1 if args.open_loop else args.primary_parallel
    config["resolved"] = {
        "parallel": resolved_parallel,
        "number": args.primary_number,
        "rate": args.primary_rate,
    }
    config["collect_engine_metrics"] = args.collect_engine_metrics
    config["engine_metrics_url"] = (
        args.engine_metrics_url
        or (args.url and "derived")
    )

    # W&B: EvalScope init_visualizer; local files under results/<timestamp>/.
    wandb = _make_wandb(
        args,
        config,
        wandb_dir=writer.output_dir,
    )
    aggregator = MetricsAggregator()
    rate = args.primary_rate

    def _on_result(partial: dict[str, Any]) -> None:
        if not wandb.active:
            return
        partial_metrics = aggregator.aggregate(partial)
        attach_user_throughput(
            partial_metrics, parallel=resolved_parallel
        )
        wandb.log_perf(
            partial_metrics,
            parallel=resolved_parallel,
            request_rate=rate,
        )

    client = VLLMClient(
        args.url,
        args.model,
        timeout=args.timeout,
        api_key=args.api_key,
    )
    runner = SingleRunner(
        client,
        requests,
        args.primary_parallel,
        number=args.primary_number,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        stream=args.stream,
        on_result=_on_result if wandb.active else None,
        rate=rate,
        open_loop=args.open_loop,
    )

    result, engine = await _collect_engine(args, runner.run)
    metrics = aggregator.aggregate(result)
    metrics["rate"] = rate
    metrics["number"] = args.primary_number
    attach_user_throughput(metrics, parallel=resolved_parallel)

    engine_summary = None
    if engine is not None:
        engine_summary = engine.get("summary")
        metrics["engine"] = engine_summary
        csv_text = engine_timeseries_csv(engine.get("timeseries") or [])
        if csv_text:
            writer.save_artifact("engine_metrics.csv", csv_text)

    print_summary(config, metrics)

    table = build_metrics_table(
        client_metrics=metrics,
        engine_summary=engine_summary,
    )

    writer.save_json("config.json", config)
    writer.save_json("raw.json", result["results"])  # per-request records
    writer.save_json(
        "summary.json",
        {
            "schema": "foretoken.single_metrics.v1",
            "metrics": metrics,
        },
    )
    writer.save_json("metrics_table.json", table)

    if wandb.active:
        wandb.log_perf(
            metrics,
            parallel=resolved_parallel,
            request_rate=rate,
        )
        wandb.finish()

    print(f"\nResults saved: {writer.output_dir}")
    return {
        "mode": "single",
        "metrics": metrics,
        "metrics_table": table,
        "output_dir": writer.output_dir,
    }


async def _run_sweep(
    args: BenchArguments,
    requests: Optional[list[dict]],
) -> dict[str, Any]:
    points = args.sweep_points()

    writer = ResultWriter(root_dir=args.outputs_dir)
    config = args.run_config()
    config["mode"] = "sweep"
    config["schema"] = "foretoken.list_sweep.v1"
    config["sweep_points"] = [
        {
            "parallel": (-1 if args.open_loop else p),
            "number": n,
            "rate": r,
        }
        for p, n, r in points
    ]
    config["resolved"] = {
        "parallel": (-1 if args.open_loop else args.primary_parallel),
        "number": args.primary_number,
        "rate": args.primary_rate,
    }
    config["gpu_count"] = args.gpu_count
    config["collect_engine_metrics"] = args.collect_engine_metrics

    writer.save_json("config.json", config)

    # List sweep: one W&B run per point, shared group for comparison.
    wandb_group = ""
    if args.wandb:
        wandb_group = default_wandb_group_name(
            model=args.model,
            run_name=args.wandb_run_name,
        )
        print(f"[wandb] list-sweep group: {wandb_group}")
    point_wandb: dict[str, Any] = {"writer": None}

    sweep = SweepRunner(
        VLLMClient,
        args.url,
        args.model,
        points=points,
        api_key=args.api_key,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        stream=args.stream,
        requests=requests,
        open_loop=args.open_loop,
    )

    async def _wrap(run_coro):
        return await _collect_engine(args, run_coro)

    aggregator = MetricsAggregator()

    def _on_point_start(parallel: int, number: int, rate: float) -> None:
        if not args.wandb:
            return
        log_parallel = -1 if args.open_loop else parallel
        point_config = dict(config)
        point_config["resolved"] = {
            "parallel": log_parallel,
            "number": number,
            "rate": rate,
        }
        point_wandb["writer"] = _make_wandb(
            args,
            point_config,
            wandb_dir=writer.output_dir,
            run_name=sweep_point_run_name(
                wandb_group,
                parallel=log_parallel,
                number=number,
                rate=rate,
                open_loop=args.open_loop,
            ),
            group=wandb_group,
            job_type="list-sweep",
        )

    def _on_result_factory(parallel: int, _number: int, rate: float):
        wb = point_wandb.get("writer")
        if wb is None or not wb.active:
            return None
        log_parallel = -1 if args.open_loop else parallel

        def _on_result(partial: dict[str, Any]) -> None:
            active = point_wandb.get("writer")
            if active is None or not active.active:
                return
            partial_metrics = aggregator.aggregate(partial)
            attach_user_throughput(
                partial_metrics, parallel=log_parallel
            )
            active.log_perf(
                partial_metrics,
                parallel=log_parallel,
                request_rate=rate,
            )

        return _on_result

    def _on_point_end(metrics: dict[str, Any]) -> None:
        wb = point_wandb.get("writer")
        if wb is None:
            return
        if wb.active:
            wb.log_perf(
                metrics,
                parallel=metrics.get("parallel"),
                request_rate=float(metrics.get("rate", -1)),
            )
            wb.finish()
        point_wandb["writer"] = None

    results = await sweep.run(
        wrap_run=_wrap,
        on_result_factory=_on_result_factory,
        on_point_start=_on_point_start,
        on_point_end=_on_point_end,
    )

    for metrics in results:
        timeseries = metrics.pop("_engine_timeseries", None)
        if timeseries:
            csv_text = engine_timeseries_csv(timeseries)
            if csv_text:
                writer.save_artifact(
                    f"engine_metrics_p{metrics['parallel']}.csv",
                    csv_text,
                )

    attach_pareto_metrics(results, gpu_count=args.gpu_count)
    print_sweep_results(results)

    frontier = pareto_frontier(results, gpu_count=args.gpu_count)
    summary = {
        "schema": "foretoken.list_sweep.v1",
        "mode": "sweep",
        "gpu_count": args.gpu_count,
        "config": config,
        "wandb_group": wandb_group or None,
        "best_throughput": max(
            results,
            key=lambda x: x["throughput"]["token/s"],
        ),
        "pareto_frontier": frontier,
        "results": results,
    }

    best = summary["best_throughput"]
    table = build_metrics_table(
        client_metrics=best,
        engine_summary=best.get("engine"),
    )

    # Aggregated per-point metrics (not per-request); do not use raw.json.
    writer.save_json("sweep_points.json", results)
    writer.save_json("summary.json", summary)
    writer.save_json("metrics_table.json", table)
    writer.save_artifact("sweep.csv", sweep_csv(results))

    plot_path = f"{writer.artifact_dir}/pareto.png"
    plot_pareto(
        results,
        plot_path,
        frontier=frontier,
        gpu_count=args.gpu_count,
    )
    backend = "vllm" if vllm_pareto_available() else "foretoken"
    print(f"\n[pareto] backend={backend} axes=tokens/s/user vs tokens/s/GPU")

    print_pareto_frontier(frontier)

    # Final user-level + system summary for the best-throughput point.
    best_config = dict(config)
    best_config["resolved"] = {
        "parallel": best.get("parallel"),
        "number": best.get("number"),
        "rate": best.get("rate", -1),
    }
    print("\nBest throughput point")
    print_summary(best_config, best)

    print(f"Results saved: {writer.output_dir}")
    return {
        "mode": "sweep",
        "summary": summary,
        "metrics_table": table,
        "output_dir": writer.output_dir,
    }
