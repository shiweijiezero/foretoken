import csv
import io
from typing import Any


def sweep_csv(results: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "parallel",
            "number",
            "rate",
            "token/s",
            "request/s",
            "p99_latency",
            "p50_ttft",
            "p50_tpot",
            "token_s_per_user",
            "token_s_per_gpu",
        ]
    )
    for r in results:
        pareto = r.get("pareto") or {}
        throughput = r.get("throughput") or {}
        tok_user = throughput.get("token/s/user")
        if tok_user is None:
            tok_user = pareto.get("token_s_per_user", "")
        writer.writerow(
            [
                r.get("parallel", r.get("concurrency", "")),
                r.get("number", ""),
                r.get("rate", ""),
                throughput.get("token/s", ""),
                throughput.get("request/s", ""),
                r.get("latency", {}).get("p99", ""),
                r.get("ttft", {}).get("p50", ""),
                r.get("tpot", {}).get("p50", ""),
                tok_user,
                pareto.get("token_s_per_gpu", ""),
            ]
        )
    return buffer.getvalue()
