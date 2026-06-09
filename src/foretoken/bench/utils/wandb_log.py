# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""可选 WandB 上报:把一次 run 的配置与关键指标推到 WandB(由 replay 的 --wandb-project 触发)。

config = 跑这次的配置(模型 / 标签 / 负载 / GPU);log = 标量指标(延迟分位 / goodput / 成本 /
吞吐 / 引擎 KV / 客户端健康),便于在 WandB 里按 group 横向对比多组实验。
"""

from __future__ import annotations

import wandb

_TIERS = ("strict", "medium", "loose")


def log_run(run: dict, *, project: str, group: str | None = None, name: str | None = None) -> None:
    """把 run dict(meta + 指标)推到 WandB;name 缺省取 tag。"""
    m, w, gpu = run["model"], run["workload"], run["gpu"]
    config = {
        "model": m["name"], "tag": run.get("tag"), "split": w.get("split"), "window": w.get("window"),
        "rate_per_min": w.get("rate_per_min"), "total_requests": w.get("total_requests"),
        "sec_multiplier": w.get("sec_multiplier"), "deadline_s": w.get("deadline_s"),
        "gpu_count": gpu.get("count"), "gpu_name": gpu.get("name"),
        "vllm": run.get("vllm"), "commit": run.get("commit"), "engine_args": m.get("engine_args"),
    }
    wb = wandb.init(project=project, group=group, name=name or run.get("tag"), config=config)
    lat, tp = run["latency"], run["throughput"]
    cost, eng, ch = run.get("cost") or {}, run.get("engine") or {}, run.get("client_health") or {}
    metrics: dict = {
        "completed": run["completed"], "total": run["total"],
        "cancelled": run["cancelled_sessions"], "duration_s": run["duration_s"],
        "tail_ratio_ttft": run.get("tail_ratio_ttft"),
        "throughput/out_tok_s": tp["output_tok_s"], "throughput/req_s": tp["request_s"],
    }
    for k in ("p50", "p90", "p99"):
        metrics[f"ttft/{k}"] = lat["ttft_ms"][k]
        metrics[f"tpot/{k}"] = lat["tpot_ms"][k]
    for k in ("p10", "p50", "p90"):  # 生成速度 1000/TPOT
        metrics[f"gen_tok_s/{k}"] = lat.get("gen_tok_s", {}).get(k)
    for i, g in enumerate(run.get("goodput", [])[:3]):
        metrics[f"goodput/{_TIERS[i]}_attain"] = g["attain"]
        metrics[f"goodput/{_TIERS[i]}_tok_s"] = g["good_tok_s"]
    for i, v in enumerate((cost.get("cny_per_mtok") or [])[:3]):
        if v is not None:
            metrics[f"cost/cny_per_mtok_{_TIERS[i]}"] = v
    if cost.get("cny_per_mtok_raw") is not None:
        metrics["cost/cny_per_mtok_raw"] = cost["cny_per_mtok_raw"]
    if cost.get("run_cost_cny") is not None:
        metrics["cost/run_cny"] = cost["run_cost_cny"]
    if eng:
        metrics["engine/peak_kv"] = eng["peak_kv"]
        metrics["engine/max_waiting"] = eng["max_waiting"]
        metrics["engine/preempted"] = eng.get("total_preempted", 0)
        if eng.get("prefix_hit_rate") is not None:
            metrics["engine/prefix_hit_rate"] = eng["prefix_hit_rate"]
        if eng.get("decode_tok_s") is not None:
            metrics["engine/decode_tok_s"] = eng["decode_tok_s"]
    er = run.get("engine_requests") or {}
    for key in ("queued_ms", "prefill_ms", "decode_ms", "tpot_ms"):  # 引擎时间分解
        if key in er:
            metrics[f"engine_req/{key}_p50"] = er[key]["p50"]
            metrics[f"engine_req/{key}_p99"] = er[key]["p99"]
    if ch.get("samples"):
        metrics["client/loop_lag_mean_ms"] = ch["loop_lag_mean_ms"]
        metrics["client/loop_lag_max_ms"] = ch["loop_lag_max_ms"]
    wb.log(metrics)
    wb.finish()
