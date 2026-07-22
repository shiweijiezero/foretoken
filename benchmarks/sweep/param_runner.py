"""Cartesian-product runner for ``--serve-params`` × ``--bench-params``.

Aligned with vLLM ``bench sweep`` JSON formats:
- list or named-dict parameter files
- ``--link-vars`` to filter product pairs
- ``--num-runs`` repeats per combination
- ``--dry-run`` prints planned runs without executing

Serve combinations are recorded as metadata only; the benchmark talks to an
already-running external server (``--url``).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.arguments import BenchArguments
from benchmarks.analyzer.pareto import (
    attach_pareto_metrics,
    pareto_frontier,
)
from benchmarks.analyzer.vllm_pareto import vllm_pareto_available
from benchmarks.sweep.pareto_plot import plot_pareto
from benchmarks.sweep.overrides import apply_bench_overrides
from benchmarks.sweep.param_sweep import (
    ParameterSweepItem,
    comb_is_valid,
    load_param_sweep,
    parse_link_vars,
    sanitize_filename,
)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _comb_dir_name(
    serve_comb: ParameterSweepItem,
    bench_comb: ParameterSweepItem,
) -> str:
    parts: list[str] = []
    if serve_comb:
        parts.extend(("SERVE", serve_comb.name))
    if bench_comb:
        parts.extend(("BENCH", bench_comb.name))
    if not parts:
        parts.append("BASE")
    return sanitize_filename("-".join(parts))


def iter_param_combinations(
    args: BenchArguments,
) -> list[tuple[ParameterSweepItem, ParameterSweepItem]]:
    serve_params = load_param_sweep(args.serve_params or None)
    bench_params = load_param_sweep(args.bench_params or None)
    link_vars = parse_link_vars(args.link_vars)
    pairs: list[tuple[ParameterSweepItem, ParameterSweepItem]] = []
    for serve_comb in serve_params:
        for bench_comb in bench_params:
            if comb_is_valid(serve_comb, bench_comb, link_vars):
                pairs.append((serve_comb, bench_comb))
    return pairs


def resolve_experiment_dir(args: BenchArguments) -> Path:
    name = (args.experiment_name or "").strip()
    if not name:
        name = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(args.outputs_dir) / name


def _print_dry_run(
    args: BenchArguments,
    pairs: list[tuple[ParameterSweepItem, ParameterSweepItem]],
    experiment_dir: Path,
) -> None:
    print("[DRY RUN] Parameter sweep plan")
    print(f"  experiment_dir : {experiment_dir}")
    print(f"  url            : {args.url}")
    print(f"  num_runs       : {args.num_runs}")
    print(f"  combinations   : {len(pairs)}")
    for index, (serve_comb, bench_comb) in enumerate(pairs):
        print(f"\n[{index}] {_comb_dir_name(serve_comb, bench_comb)}")
        print(f"  serve: {dict(serve_comb) or '{}'}")
        print(f"  bench: {dict(bench_comb) or '{}'}")


async def run_param_sweep(args: BenchArguments) -> dict[str, Any]:
    """Execute serve×bench Cartesian product; delegates each point to run_benchmark."""
    # Local import avoids circular import with main.run_benchmark
    from benchmarks.main import run_benchmark

    if args.num_runs < 1:
        raise ValueError("--num-runs must be >= 1")

    pairs = iter_param_combinations(args)
    if not pairs:
        raise ValueError(
            "No valid serve×bench combinations after applying --link-vars"
        )

    experiment_dir = resolve_experiment_dir(args)
    if args.dry_run:
        _print_dry_run(args, pairs, experiment_dir)
        return {
            "mode": "param_sweep",
            "dry_run": True,
            "combinations": len(pairs),
            "experiment_dir": str(experiment_dir),
        }

    experiment_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        experiment_dir / "param_sweep_config.json",
        {
            "serve_params": args.serve_params,
            "bench_params": args.bench_params,
            "link_vars": args.link_vars,
            "num_runs": args.num_runs,
            "base": args.to_dict(),
            "combinations": [
                {
                    "serve": dict(s),
                    "bench": dict(b),
                    "dir": _comb_dir_name(s, b),
                }
                for s, b in pairs
            ],
        },
    )

    all_rows: list[dict[str, Any]] = []
    combo_summaries: list[dict[str, Any]] = []

    for serve_comb, bench_comb in pairs:
        comb_name = _comb_dir_name(serve_comb, bench_comb)
        comb_root = experiment_dir / comb_name
        comb_root.mkdir(parents=True, exist_ok=True)

        run_results: list[dict[str, Any]] = []
        for run_number in range(args.num_runs):
            print("=" * 60)
            print(
                f"PARAM SWEEP {comb_name} "
                f"run={run_number + 1}/{args.num_runs}"
            )
            print(f"  serve: {dict(serve_comb) or '{}'}")
            print(f"  bench: {dict(bench_comb) or '{}'}")
            print("=" * 60)

            point_args = apply_bench_overrides(args, bench_comb)
            point_args = replace(
                point_args,
                outputs_dir=str(comb_root / f"run={run_number}"),
                # Clear flags so nested run_benchmark does single/sweep.
                serve_params="",
                bench_params="",
                dry_run=False,
                num_runs=1,
            )

            result = await run_benchmark(point_args)
            extracted = _extract_metric_rows(
                result,
                comb_name=comb_name,
                run_number=run_number,
                serve_comb=serve_comb,
                bench_comb=bench_comb,
                point_args=point_args,
            )
            run_results.extend(extracted)
            all_rows.extend(extracted)

        summary_path = comb_root / "summary.json"
        _write_json(
            summary_path,
            {
                "schema": "foretoken.param_combo_rows.v1",
                "rows": run_results,
            },
        )
        combo_summaries.append(
            {
                "combination": comb_name,
                "runs": len(run_results),
                "summary": str(summary_path),
            }
        )

    csv_text = _rows_to_csv(all_rows)
    (experiment_dir / "summary.csv").write_text(csv_text, encoding="utf-8")
    _write_json(
        experiment_dir / "summary.json",
        {
            "schema": "foretoken.param_sweep_rows.v1",
            "rows": all_rows,
        },
    )

    pareto_info = _plot_param_sweep_pareto(
        all_rows,
        experiment_dir,
        gpu_count=args.gpu_count,
    )

    print(f"\nParam sweep saved: {experiment_dir}")
    if pareto_info.get("plot_path"):
        print(f"Pareto plot: {pareto_info['plot_path']}")
    return {
        "mode": "param_sweep",
        "experiment_dir": str(experiment_dir),
        "combinations": len(pairs),
        "rows": all_rows,
        "combo_summaries": combo_summaries,
        "pareto": pareto_info,
    }


def _metric_blob_to_row(
    metrics: dict[str, Any],
    *,
    comb_name: str,
    run_number: int,
    serve_comb: ParameterSweepItem,
    bench_comb: ParameterSweepItem,
    point_args: BenchArguments,
    mode: str,
    output_dir: Any,
) -> dict[str, Any]:
    from benchmarks.arguments import metric_parallel
    from benchmarks.metrics.aggregator import (
        attach_user_throughput,
        tokens_per_s_per_user,
    )

    conc = metric_parallel(
        metrics,
        default=point_args.primary_parallel or 1,
    )
    number = metrics.get("number") or point_args.primary_number
    token_s = float(metrics.get("throughput", {}).get("token/s", 0.0) or 0.0)
    request_s = float(metrics.get("throughput", {}).get("request/s", 0.0) or 0.0)
    tok_user = metrics.get("throughput", {}).get("token/s/user")
    if tok_user is None:
        tok_user = tokens_per_s_per_user(token_s, conc)
    else:
        tok_user = float(tok_user)
    p99 = float(metrics.get("latency", {}).get("p99", 0.0) or 0.0)
    row: dict[str, Any] = {
        "combination": comb_name,
        "run_number": run_number,
        "serve": dict(serve_comb),
        "bench": dict(bench_comb),
        "output_dir": output_dir,
        "mode": mode,
        # Canonical metric field (aliases only under bench.* / serve.*)
        "parallel": conc,
        "number": number,
        "token_s": token_s,
        "request_s": request_s,
        "p99_latency": p99,
        "success_rate": metrics.get("success_rate"),
        "gpu_count": point_args.gpu_count,
        "throughput": {
            "token/s": token_s,
            "request/s": request_s,
            "token/s/user": float(tok_user),
        },
        "latency": {
            "p99": p99,
            "p50": float(metrics.get("latency", {}).get("p50", 0.0) or 0.0),
            "p95": float(metrics.get("latency", {}).get("p95", 0.0) or 0.0),
            "mean": float(metrics.get("latency", {}).get("mean", 0.0) or 0.0),
        },
        "label": f"{comb_name}|p={conc}",
        "serve_name": serve_comb.name if serve_comb else "BASE",
        "bench_name": bench_comb.name if bench_comb else "BASE",
    }
    attach_user_throughput(row, parallel=conc)
    # Prefixed copies only — do not dual-write alias keys at top level
    # (max_concurrency / concurrency / num_prompts stay under bench.* / serve.*).
    _top_level_skip = {
        "_benchmark_name",
        "max_concurrency",
        "concurrency",
        "num_prompts",
        "parallel",
        "number",
    }
    for key, value in dict(serve_comb).items():
        if key != "_benchmark_name":
            row[f"serve.{key}"] = value
            if key not in _top_level_skip:
                row.setdefault(key, value)
    for key, value in dict(bench_comb).items():
        if key != "_benchmark_name":
            row[f"bench.{key}"] = value
            if key not in _top_level_skip:
                row.setdefault(key, value)
    return row


def _extract_metric_rows(
    result: dict[str, Any],
    *,
    comb_name: str,
    run_number: int,
    serve_comb: ParameterSweepItem,
    bench_comb: ParameterSweepItem,
    point_args: BenchArguments,
) -> list[dict[str, Any]]:
    """Flatten single / nested-sweep results into summary rows."""
    mode = str(result.get("mode") or "single")
    output_dir = result.get("output_dir")

    if mode == "sweep":
        points = (result.get("summary") or {}).get("results") or []
        if points:
            return [
                _metric_blob_to_row(
                    metrics,
                    comb_name=comb_name,
                    run_number=run_number,
                    serve_comb=serve_comb,
                    bench_comb=bench_comb,
                    point_args=point_args,
                    mode=mode,
                    output_dir=output_dir,
                )
                for metrics in points
            ]

    metrics = result.get("metrics") or {}
    if not metrics and result.get("summary"):
        metrics = result["summary"].get("best_throughput") or {}
    if not metrics:
        return [
            {
                "combination": comb_name,
                "run_number": run_number,
                "serve": dict(serve_comb),
                "bench": dict(bench_comb),
                "output_dir": output_dir,
                "mode": mode,
            }
        ]
    return [
        _metric_blob_to_row(
            metrics,
            comb_name=comb_name,
            run_number=run_number,
            serve_comb=serve_comb,
            bench_comb=bench_comb,
            point_args=point_args,
            mode=mode,
            output_dir=output_dir,
        )
    ]


def _mean_rows_by_point(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average repeated num_runs for the same combination+parallel."""
    from benchmarks.arguments import metric_parallel
    from benchmarks.metrics.aggregator import attach_user_throughput

    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("token_s") is None and not row.get("throughput"):
            continue
        key = (row.get("combination"), metric_parallel(row))
        groups.setdefault(key, []).append(row)

    averaged: list[dict[str, Any]] = []
    for group in groups.values():
        base = dict(group[0])
        n = len(group)
        token_s = sum(float(r.get("token_s") or 0.0) for r in group) / n
        request_s = sum(float(r.get("request_s") or 0.0) for r in group) / n
        p99 = sum(float(r.get("p99_latency") or 0.0) for r in group) / n
        conc = metric_parallel(base)
        gpu_count = int(base.get("gpu_count") or 1)
        base.update(
            {
                "token_s": token_s,
                "request_s": request_s,
                "p99_latency": p99,
                "parallel": conc,
                "gpu_count": gpu_count,
                "throughput": {"token/s": token_s, "request/s": request_s},
                "latency": {"p99": p99, "p50": p99, "p95": p99, "mean": p99},
                "label": base.get("label") or f"{base.get('combination')}|p={conc}",
                "num_runs_averaged": n,
            }
        )
        attach_user_throughput(base, parallel=conc)
        # Drop legacy dual-write keys if present on averaged rows.
        base.pop("max_concurrency", None)
        base.pop("concurrency", None)
        base.pop("output_throughput", None)
        averaged.append(base)
    return averaged


def _group_key_name(row: dict[str, Any], axis: str) -> str:
    if axis == "serve":
        serve = row.get("serve") or {}
        return str(
            row.get("serve_name")
            or serve.get("_benchmark_name")
            or "BASE"
        )
    if axis == "bench":
        bench = row.get("bench") or {}
        return str(
            row.get("bench_name")
            or bench.get("_benchmark_name")
            or "BASE"
        )
    if axis == "combination":
        return str(row.get("combination") or "BASE")
    raise ValueError(f"Unknown pareto group axis: {axis}")


def _write_pareto_plot(
    points: list[dict[str, Any]],
    output: Path,
    *,
    gpu_count: int,
    title: str,
    label_by: list[str],
) -> dict[str, Any]:
    if not points:
        return {"skipped": True, "reason": "empty", "plot_path": str(output)}

    attach_pareto_metrics(points, gpu_count=gpu_count)
    frontier = pareto_frontier(points, gpu_count=gpu_count)
    plot_pareto(
        points,
        str(output),
        frontier=frontier,
        gpu_count=gpu_count,
        label_by=label_by,
        title=title,
    )
    return {
        "plot_path": str(output),
        "title": title,
        "points": len(points),
        "frontier_size": len(frontier),
        "frontier": frontier,
    }


def _plot_param_sweep_pareto(
    rows: list[dict[str, Any]],
    experiment_dir: Path,
    *,
    gpu_count: int,
) -> dict[str, Any]:
    """Write overall + per-group Pareto plots (tokens/s/user vs tokens/s/GPU)."""
    points = _mean_rows_by_point(rows)
    if len(points) < 1:
        print("[pareto] skip: no metric points in param sweep")
        return {"skipped": True, "reason": "no_points"}

    pareto_dir = experiment_dir / "pareto"
    # vLLM adapter maps parallel → max_concurrency for user_count_var;
    # label with canonical foretoken field ``parallel``.
    label_by = ["combination", "parallel", "gpu_count"]
    plots: list[dict[str, Any]] = []

    overall = _write_pareto_plot(
        points,
        experiment_dir / "pareto.png",
        gpu_count=gpu_count,
        title="Param sweep Pareto (all)",
        label_by=label_by,
    )
    plots.append({"group": "all", **overall})

    for axis, subdir, label_fields in (
        ("serve", "by_serve", ["bench_name", "parallel", "gpu_count"]),
        ("bench", "by_bench", ["serve_name", "parallel", "gpu_count"]),
        ("combination", "by_combination", ["parallel", "gpu_count"]),
    ):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for point in points:
            grouped.setdefault(_group_key_name(point, axis), []).append(point)

        if axis != "combination" and len(grouped) <= 1:
            continue

        out_root = pareto_dir / subdir
        wrote_any = False
        for name, group_points in sorted(grouped.items()):
            if axis == "combination" and len(group_points) < 2:
                continue
            if not group_points:
                continue
            out_root.mkdir(parents=True, exist_ok=True)
            wrote_any = True
            safe = sanitize_filename(str(name)) or "unnamed"
            info = _write_pareto_plot(
                group_points,
                out_root / f"{safe}.png",
                gpu_count=gpu_count,
                title=f"Pareto by {axis}={name}",
                label_by=label_fields,
            )
            plots.append({"group": axis, "name": name, **info})
        if not wrote_any:
            continue

    frontier_payload = {
        "axes": {"x": "tokens/s/user", "y": "tokens/s/GPU"},
        "gpu_count": gpu_count,
        "backend": "vllm" if vllm_pareto_available() else "foretoken",
        "plots": [
            {
                "group": p.get("group"),
                "name": p.get("name"),
                "plot_path": p.get("plot_path"),
                "points": p.get("points"),
                "frontier_size": p.get("frontier_size"),
            }
            for p in plots
        ],
        "overall_frontier": overall.get("frontier"),
        "points": points,
    }
    _write_json(experiment_dir / "pareto_frontier.json", frontier_payload)

    print(f"\nPareto plots (tokens/s/user vs tokens/s/GPU): {len(plots)} figure(s)")
    print(f"  overall: {overall.get('plot_path')}")
    for item in plots:
        if item.get("group") == "all":
            continue
        print(
            f"  {item.get('group')}={item.get('name')}: "
            f"{item.get('plot_path')} "
            f"(points={item.get('points')}, frontier={item.get('frontier_size')})"
        )

    from benchmarks.report.summary import print_pareto_frontier

    # Adapt param-sweep frontier rows (may use combination label).
    adapted: list[dict[str, Any]] = []
    for item in overall.get("frontier") or []:
        row = dict(item)
        if row.get("parallel") is None and row.get("label"):
            row.setdefault("parallel", row.get("label"))
        adapted.append(row)
    print_pareto_frontier(adapted)

    return {
        "plot_path": overall.get("plot_path"),
        "plots": plots,
        "frontier_size": overall.get("frontier_size"),
        "points": len(points),
    }


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    preferred = [
        "combination",
        "run_number",
        "mode",
        "parallel",
        "number",
        "token_s",
        "request_s",
        "p99_latency",
        "success_rate",
        "gpu_count",
        "output_dir",
    ]
    keys: list[str] = []
    for key in preferred:
        if any(key in row for row in rows):
            keys.append(key)
    for row in rows:
        for key in row:
            if key in ("serve", "bench"):
                continue
            if key not in keys:
                keys.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in keys})
    return buffer.getvalue()
