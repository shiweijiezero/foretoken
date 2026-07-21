from __future__ import annotations

from typing import Any, Optional


def build_metrics_table(
    client_metrics: Optional[dict[str, Any]] = None,
    engine_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build frontend-friendly metrics table: columns + rows."""
    rows: list[list[Any]] = []
    if client_metrics:
        rows.extend(_client_rows(client_metrics))
    if engine_summary:
        rows.extend(_engine_rows(engine_summary))
    return {"columns": ["metric", "group", "value", "unit"], "rows": rows}


def _stat_rows(
    prefix: str,
    group: str,
    stats: dict[str, Any],
    unit: str,
) -> list[list[Any]]:
    rows = []
    for key in ("mean", "p50", "p95", "p99", "max", "min", "last"):
        if key in stats and stats[key] is not None:
            rows.append([f"{prefix}_{key}", group, float(stats[key]), unit])
    return rows


def _client_rows(metrics: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for key in ("request_num", "success_num", "failed_num", "success_rate"):
        if key in metrics:
            unit = "ratio" if key == "success_rate" else "count"
            rows.append([key, "client", metrics[key], unit])

    for name, unit in (("latency", "s"), ("ttft", "s"), ("tpot", "s")):
        if name in metrics and isinstance(metrics[name], dict):
            rows.extend(_stat_rows(name, "client", metrics[name], unit))

    throughput = metrics.get("throughput") or {}
    if "request/s" in throughput:
        rows.append(
            ["request_per_s", "client", float(throughput["request/s"]), "1/s"]
        )
    if "token/s" in throughput:
        rows.append(
            ["token_per_s", "client", float(throughput["token/s"]), "1/s"]
        )
    if "token/s/user" in throughput:
        rows.append(
            [
                "token_per_s_per_user",
                "client",
                float(throughput["token/s/user"]),
                "1/s",
            ]
        )
    if "benchmark_time" in metrics:
        rows.append(
            ["benchmark_time", "client", float(metrics["benchmark_time"]), "s"]
        )
    return rows


def _engine_rows(summary: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    if "sample_count" in summary:
        rows.append(
            ["engine_sample_count", "engine", summary["sample_count"], "count"]
        )

    for name, unit in (
        ("num_requests_running", "count"),
        ("num_requests_waiting", "count"),
        ("kv_cache_usage_perc", "ratio"),
    ):
        stats = summary.get(name)
        if isinstance(stats, dict):
            rows.extend(_stat_rows(name, "engine", stats, unit))

    hit_rate = summary.get("prefix_cache_hit_rate")
    if hit_rate is not None:
        rows.append(
            ["prefix_cache_hit_rate", "engine", float(hit_rate), "ratio"]
        )

    for key, value in (summary.get("counter_delta") or {}).items():
        rows.append([f"{key}_delta", "engine", float(value), "count"])
    return rows
