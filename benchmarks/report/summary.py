"""Print a single-run metrics summary to stdout (tabular)."""

from __future__ import annotations

from typing import Any

from benchmarks.metrics.aggregator import (
    attach_user_throughput,
    tokens_per_s_per_user,
)
from benchmarks.report.console import (
    fmt_float,
    percentile_metric_rows,
    print_kv_table,
    print_table,
)


def _tok_s_per_user(metrics: dict[str, Any]) -> float:
    """Resolve ``token/s/user`` for display (compute if missing)."""
    throughput = metrics.get("throughput") or {}
    if "token/s/user" in throughput:
        return float(throughput["token/s/user"])
    token_s = float(throughput.get("token/s") or 0.0)
    parallel = metrics.get("parallel")
    if parallel is None and metrics.get("concurrency") is not None:
        parallel = metrics.get("concurrency")
    return tokens_per_s_per_user(token_s, parallel)


def _resolve_parallel(
    config: dict[str, Any], metrics: dict[str, Any]
) -> Any:
    resolved = config.get("resolved") or {}
    parallel = metrics.get("parallel")
    if parallel is None:
        parallel = resolved.get("parallel")
    if parallel is None:
        cfg_p = config.get("parallel")
        if isinstance(cfg_p, list):
            parallel = cfg_p[0] if cfg_p else None
        else:
            parallel = cfg_p
    return parallel


def _resolve_number(config: dict[str, Any]) -> Any:
    resolved = config.get("resolved") or {}
    number = resolved.get("number")
    if number is None:
        number = config.get("number")
        if isinstance(number, list):
            number = number[0] if number else ""
    return number


def print_summary(config: dict[str, Any], metrics: dict[str, Any]) -> None:
    parallel = _resolve_parallel(config, metrics)
    attach_user_throughput(metrics, parallel=parallel)

    if parallel is not None and int(parallel) < 0:
        parallel_s = "unlimited (open-loop)"
    else:
        parallel_s = parallel

    print()
    print("╔══════════════════════════════════════════╗")
    print("║       Foretoken Benchmark Result         ║")
    print("╚══════════════════════════════════════════╝")

    config_rows: list[tuple[str, Any]] = [
        ("Model", config.get("model", "")),
        ("Requests", _resolve_number(config)),
        ("Parallel", parallel_s),
    ]
    if config.get("open_loop"):
        config_rows.append(("Open Loop", True))
    if "stream" in config:
        config_rows.append(("Stream", config["stream"]))
    rate = (config.get("resolved") or {}).get("rate", config.get("rate"))
    if rate is not None:
        if isinstance(rate, list):
            rate = rate[0] if rate else None
        if rate is not None and float(rate) > 0:
            config_rows.append(("Rate (req/s)", rate))
    print_kv_table(config_rows, title="\nConfiguration")

    print_kv_table(
        [
            ("Total", metrics.get("request_num", 0)),
            ("Success", metrics.get("success_num", 0)),
            ("Failed", metrics.get("failed_num", 0)),
            (
                "Success Rate",
                f"{float(metrics.get('success_rate') or 0.0) * 100:.2f}%",
            ),
        ],
        title="\nRequest",
    )

    user_rows = percentile_metric_rows(
        metrics,
        [
            ("latency", "Latency", "s"),
            ("ttft", "TTFT", "s"),
            ("tpot", "TPOT", "s"),
        ],
    )
    user_rows.append(
        ["Tok/s/user", fmt_float(_tok_s_per_user(metrics)), "—", "—", "—"]
    )
    print_table(
        ["Metric", "mean", "p50", "p95", "p99"],
        user_rows,
        aligns=["left", "right", "right", "right", "right"],
        title="\nUser-level",
    )

    throughput = metrics.get("throughput") or {}
    system_rows: list[tuple[str, Any]] = [
        ("Request/s", fmt_float(throughput.get("request/s"))),
        ("Token/s", fmt_float(throughput.get("token/s"))),
        ("Tok/s/user", fmt_float(_tok_s_per_user(metrics))),
    ]
    pareto = metrics.get("pareto") or {}
    if pareto.get("token_s_per_gpu") is not None:
        system_rows.append(
            ("Tok/s/GPU", fmt_float(pareto.get("token_s_per_gpu")))
        )
    print_kv_table(system_rows, title="\nSystem throughput")

    engine = metrics.get("engine")
    if isinstance(engine, dict) and engine.get("sample_count"):
        engine_rows: list[tuple[str, Any]] = [
            ("Samples", engine["sample_count"]),
        ]
        kv = engine.get("kv_cache_usage_perc") or {}
        if kv:
            engine_rows.append(("KV cache mean", fmt_float(kv.get("mean"))))
            engine_rows.append(("KV cache max", fmt_float(kv.get("max"))))
        running = engine.get("num_requests_running") or {}
        waiting = engine.get("num_requests_waiting") or {}
        if running:
            engine_rows.append(
                ("Running mean", fmt_float(running.get("mean")))
            )
        if waiting:
            engine_rows.append(
                ("Waiting mean", fmt_float(waiting.get("mean")))
            )
        hit = engine.get("prefix_cache_hit_rate")
        if hit is not None:
            engine_rows.append(("Prefix hit rate", fmt_float(hit)))
        print_kv_table(engine_rows, title="\nEngine")

    print_kv_table(
        [("Time (s)", fmt_float(metrics.get("benchmark_time")))],
        title="\nDuration",
    )
    print()


def print_sweep_results(results: list[dict[str, Any]]) -> None:
    """Print list-sweep points as a wide results table."""
    rows: list[list[Any]] = []
    for r in results:
        rate = r.get("rate", -1)
        rate_s = (
            "INF" if rate is None or float(rate) <= 0 else f"{float(rate):g}"
        )
        p = r.get("pareto") or {}
        tp = r.get("throughput") or {}
        tok_user = tp.get("token/s/user")
        if tok_user is None:
            tok_user = p.get("token_s_per_user", 0.0)
        lat = r.get("latency") or {}
        ttft = r.get("ttft") or {}
        parallel = r.get("parallel", "")
        if parallel is not None and int(parallel) < 0:
            parallel = "open"
        rows.append(
            [
                parallel,
                rate_s,
                r.get("number", ""),
                fmt_float(tp.get("token/s"), 2),
                fmt_float(tok_user, 2),
                fmt_float(p.get("token_s_per_gpu"), 2),
                fmt_float(lat.get("p99"), 3),
                fmt_float(ttft.get("p50"), 3),
                fmt_float(lat.get("mean"), 3),
            ]
        )
    print_table(
        [
            "Parallel",
            "Rate",
            "Number",
            "Token/s",
            "Tok/s/user",
            "Tok/s/GPU",
            "P99 Lat",
            "P50 TTFT",
            "Mean Lat",
        ],
        rows,
        aligns=[
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
            "right",
        ],
        title="\nSweep Result",
    )


def print_pareto_frontier(frontier: list[dict[str, Any]]) -> None:
    """Print Pareto frontier points as a compact table."""
    if not frontier:
        print("\nPareto Frontier: (empty)")
        return
    rows: list[list[Any]] = []
    for r in frontier:
        p = r.get("pareto") or {}
        tp = r.get("throughput") or {}
        tok_user = tp.get("token/s/user", p.get("token_s_per_user", 0))
        rate = r.get("rate", -1)
        rate_s = (
            "INF" if rate is None or float(rate) <= 0 else f"{float(rate):g}"
        )
        parallel = r.get("parallel", "")
        label = r.get("label") or r.get("combination")
        try:
            if parallel is not None and int(parallel) < 0:
                parallel = "open"
        except (TypeError, ValueError):
            parallel = label or parallel
        if label and str(parallel) != str(label) and not str(parallel).isdigit():
            display = label
        else:
            display = parallel
        rows.append(
            [
                display,
                rate_s,
                fmt_float(tok_user, 2),
                fmt_float(p.get("token_s_per_gpu"), 2),
                fmt_float((r.get("latency") or {}).get("p99"), 3),
            ]
        )
    print_table(
        ["Point", "Rate", "Tok/s/user", "Tok/s/GPU", "P99 Lat"],
        rows,
        aligns=["left", "right", "right", "right", "right"],
        title="\nPareto Frontier",
    )
