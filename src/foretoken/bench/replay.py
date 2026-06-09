# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""闭环会话回放的命令行入口:解析参数 → 构造负载 → 起引擎回放 → 写记录。

三种后端:缺省**进程内**(`core.vllm_engine` 自起 AsyncLLM、退出释放 GPU);
`--endpoint` 打已有 `vllm serve`(API 形式);`--serve` 自起 vllm serve、回放完整组 kill 释放 GPU。
核心见 `core/` 与 `report/`;vllm / torch 仅进程内后端需要,故在该分支体内按需 import。

运行:`python -m foretoken.bench.replay --help`(或 scripts/bench.sh)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from foretoken.bench import report
from foretoken.bench.core.backend import HttpBackend
from foretoken.bench.core.loop import replay as replay_loop
from foretoken.bench.core.serve import vllm_serve
from foretoken.bench.core.types import TurnResult
from foretoken.bench.core.workload import (
    deadline_seconds,
    group_sessions,
    load_rows,
    parse_window,
    sample_sessions,
)
from foretoken.config import read, resolve

_DEFAULT_SLO = ["2000:80", "10000:150", "60000:200"]  # 缺省 SLO 阶梯(严/中/松);--slo 整体替换


def _summary(results: list[TurnResult]) -> None:
    """打印 TTFT/TPOT 分位 + 完成数(简版;详细汇总 / 图见 report/)。"""
    ok = [r for r in results if r.ok]

    def pct(xs: list[float], q: float) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0

    ttfts = [r.ttft_ms for r in ok]
    tpots = [r.tpot_ms for r in ok if r.tpot_ms > 0]
    print(f"请求 {len(results)}(ok {len(ok)})")
    if ttfts:
        d = f"p50={pct(ttfts, 0.5):.0f} p90={pct(ttfts, 0.9):.0f} p99={pct(ttfts, 0.99):.0f}"
        print(f"TTFT ms  {d}")
    if tpots:
        d = f"p50={pct(tpots, 0.5):.0f} p90={pct(tpots, 0.9):.0f} p99={pct(tpots, 0.99):.0f}"
        print(f"TPOT ms  {d}")


def _cfg_path(args):
    """配置文件路径:--config 直接指定的文件,缺省 default.toml(见 foretoken.config)。"""
    return resolve(args.config)


def _cfg_name(args) -> str:
    """报告 / 目录用逻辑名:有 --config 用其文件名 stem,否则取 --model 的 basename。"""
    return Path(args.config).stem if args.config else Path(args.model).name


def _overlay(base: dict, kvs: list[str]) -> dict:
    """把 K=V 列表(值按 JSON 解析,失败按字符串)覆盖进 base。"""
    for kv in kvs:
        k, _, v = kv.partition("=")
        try:
            base[k] = json.loads(v)
        except json.JSONDecodeError:
            base[k] = v
    return base


def _build_sampling(args) -> dict:
    """组装采样:config 官方值 → --param 透传/覆盖任意 vLLM 采样参数 → 固定 seed。"""
    sampling = _overlay(read(_cfg_path(args)), args.param)
    sampling["seed"] = args.seed
    return sampling


def _build_engine_kwargs(args) -> dict:
    """组装引擎参数:config [serve] → --engine-param 透传/覆盖任意 AsyncEngineArgs 字段。"""
    serve = read(_cfg_path(args), "serve")
    serve["model"] = args.model
    serve.setdefault("seed", args.seed)
    return _overlay(serve, args.engine_param)


def _gpu_info(hint: int) -> dict:
    """主进程查可见 GPU 数 / 单卡显存(归一化 goodput 用);失败回退 hint(=TP)。"""
    try:
        import torch  # 惰性:仅进程内 / serve 后端用到(API 形式无 GPU 句柄)

        n = torch.cuda.device_count()
        p = torch.cuda.get_device_properties(0)
        return {
            "count": n,
            "name": p.name,
            "bytes_per_gpu": p.total_memory,
            "total_bytes": n * p.total_memory,
        }
    except Exception:  # noqa: BLE001
        return {"count": hint, "name": "?", "bytes_per_gpu": 0, "total_bytes": 0}


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001  经 rsync 同步的目录无 .git
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="进程内闭环回放 foretoken-trace,测 TTFT/TPOT/goodput")
    ap.add_argument("--model", required=True, help="权重目录或 HF id(起引擎 + 分词器)")
    ap.add_argument(
        "--config",
        default=None,
        help="配置文件路径(任意位置,[sampling]+[serve]);缺省 config/models/default.toml",
    )
    ap.add_argument(
        "--dataset",
        default="weijiezz/foretoken-trace",
        help="HF 数据集 id,或 .jsonl/.parquet 文件 / 目录(schema 见 workload.load_rows)",
    )
    ap.add_argument(
        "--split",
        default=None,
        help="HF 数据集配置名(conversation/mooncake/toolagent);本机文件可省",
    )
    ap.add_argument("--window", default=None, help="时间窗(分钟):N 或 A:B")
    ap.add_argument(
        "--rate",
        type=float,
        default=None,
        metavar="REQ_PER_MIN",
        help="到达率 req/min(优先于 --total-requests):total_requests=rate×window 分钟数",
    )
    ap.add_argument(
        "--total-requests",
        type=int,
        default=None,
        help="目标 request(轮)总数:会话级下采样到此量,匹配单实例硬件(整会话保留)",
    )
    ap.add_argument(
        "--sample",
        type=float,
        default=None,
        help="会话级下采样比例 (0,1](与 --total-requests / --rate 互斥)",
    )
    ap.add_argument("--seed", type=int, default=0, help="采样 seed(固定以可复现)")
    ap.add_argument(
        "--sec-multiplier",
        type=float,
        default=1.0,
        help="时间缩放(<1 加速回放、压缩真实间隔提并发;1=真实节奏)",
    )
    ap.add_argument(
        "--tail-factor",
        type=float,
        default=2.0,
        help="墙钟上限的乘数 = 窗口跨度 × sec_multiplier × 此值;到点取消运行中请求(截长尾)。<=0 不乘",
    )
    ap.add_argument(
        "--tail-grace",
        type=float,
        default=0.0,
        metavar="MIN",
        help="墙钟上限的加法宽限(分钟):上限 = 窗口跨度×tail-factor + 此值;与 --tail-factor 叠加",
    )
    ap.add_argument(
        "--deadline",
        type=float,
        default=None,
        metavar="SEC",
        help="回放墙钟上限秒数:直接指定(覆盖 --tail-factor 的窗口推算);省略则按 tail-factor",
    )
    ap.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="透传任意 vLLM 采样参数(可重复),如 --param repetition_penalty=1.1",
    )
    ap.add_argument(
        "--engine-param",
        action="append",
        default=[],
        metavar="K=V",
        help="透传任意 AsyncEngineArgs 字段(可重复),如 --engine-param kv_cache_dtype=fp8",
    )
    ap.add_argument(
        "--slo",
        action="append",
        default=None,
        metavar="TTFT_ms:TPOT_ms",
        help="goodput 的 SLO 阶梯 TTFT_ms:TPOT_ms(可重复,替换缺省);"
        "缺省严/中/松三档 2000:80 / 10000:150 / 60000:200",
    )
    ap.add_argument(
        "--tag",
        default="vllm-default",
        help="优化变体标签(自描述,排行榜区分):vllm-default(stock 基线)/ kv-aware / mtp / kv+mtp",
    )
    ap.add_argument(
        "--endpoint",
        default=None,
        metavar="URL",
        help="API 形式:打已有 vllm serve 地址(如 http://localhost:8000);省略则进程内自起引擎",
    )
    ap.add_argument(
        "--serve",
        action="store_true",
        help="自起 vllm serve 跑 API 形式,回放完整组 kill 释放 GPU(引擎配置取 [serve])",
    )
    ap.add_argument("--port", type=int, default=18000, help="--serve 的监听端口(默认 18000)")
    ap.add_argument("--dp", type=int, default=None, help="--serve:data-parallel 引擎副本数(-dp)")
    ap.add_argument(
        "--api-server-count", type=int, default=None, help="--serve:前端 API server 进程数"
    )
    ap.add_argument(
        "--serve-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="--serve:透传任意 vllm serve 参数(可重复),如 --serve-arg=--enforce-eager",
    )
    ap.add_argument(
        "--gpus",
        type=int,
        default=None,
        help="API 形式下服务器 GPU 数(算 goodput/GPU;远端显存测不到,归一化按字节记 None)",
    )
    ap.add_argument(
        "--cases",
        choices=["off", "sample", "full"],
        default="sample",
        help="逐轮输入输出:off 不存 / sample 仅 cases.md / full 加全量 cases.jsonl(大 run 慎用)",
    )
    ap.add_argument(
        "--runs-dir", default="results/runs", help="单实验记录根目录(默认 results/runs/)"
    )
    return ap


def _run_http(sessions, model, sampling, endpoint, *, sec_multiplier, deadline_s):
    """API 形式回放:HttpBackend 打 vllm serve,finally 关 client(连接不泄漏)。"""
    backend = HttpBackend(endpoint, model, sampling)

    async def _go():
        t = time.perf_counter()
        try:
            res, canc = await replay_loop(
                sessions, backend, sec_multiplier=sec_multiplier, deadline_s=deadline_s
            )
        finally:
            await backend.aclose()  # 及时关闭 client(连接/会话不泄漏)
        return res, canc, time.perf_counter() - t

    return asyncio.run(_go())


def main() -> None:
    args = _build_parser().parse_args()
    rows = load_rows(args.dataset, args.split)
    window = parse_window(args.window)
    span_min = (window[1] - window[0]) / 60_000.0 if window else None
    n_req = round(args.rate * span_min) if (args.rate and span_min) else args.total_requests
    sessions = group_sessions(rows, window=window)
    sessions = sample_sessions(  # 会话级下采样匹配硬件(rate→n 见上)
        sessions, n_requests=n_req, fraction=args.sample, seed=args.seed
    )
    deadline = (
        args.deadline
        if args.deadline is not None
        else deadline_seconds(window, args.sec_multiplier, args.tail_factor, args.tail_grace * 60)
    )
    n_turns = sum(len(s) for s in sessions.values())
    span_s = (window[1] - window[0]) / 1000.0 if window else None
    offered_turn_s = (n_turns / span_s) if span_s else None  # 提供负载(轮/s),扫描曲线 x 轴
    dl = f"{deadline:.0f}s" if deadline else "无"
    sampling = _build_sampling(args)
    name = _cfg_name(args)
    print(
        f"{name} [{'API ' + args.endpoint if args.endpoint else '进程内'}]: "
        f"{len(sessions)} 会话, {n_turns} 轮 | rate={args.rate} total_requests={n_req} | "
        f"时限 {dl} | 采样 {sampling}"
    )

    vllm_ver = None
    engine_stats = None
    if args.serve:  # 自起 vllm serve 子进程跑 API 形式,退出整组 kill 释放 GPU
        serve_cfg = read(_cfg_path(args), "serve")
        engine_kwargs = {
            k: v
            for k, v in {**serve_cfg, "data_parallel_size": args.dp,
                         "api_server_count": args.api_server_count}.items()
            if v is not None
        }
        gpu = _gpu_info((args.dp or 1) * serve_cfg.get("tensor_parallel_size", 1))
        with vllm_serve(
            args.model, serve_cfg, port=args.port, dp=args.dp,
            api_server_count=args.api_server_count, serve_args=args.serve_arg,
        ) as endpoint:
            results, cancelled, duration = _run_http(
                sessions, args.model, sampling, endpoint,
                sec_multiplier=args.sec_multiplier, deadline_s=deadline,
            )
    elif args.endpoint:  # 打已有 vllm serve(引擎在服务器侧,无逐 iteration 监控)
        engine_kwargs = {}
        gpu = {"count": args.gpus or 0, "name": "remote", "bytes_per_gpu": 0, "total_bytes": 0}
        results, cancelled, duration = _run_http(
            sessions, args.model, sampling, args.endpoint,
            sec_multiplier=args.sec_multiplier, deadline_s=deadline,
        )
    else:  # 进程内自起引擎(vllm 仅此分支需要 → 惰性 import)
        import vllm

        from foretoken.bench.core.vllm_engine import run_replay

        vllm_ver = vllm.__version__
        engine_kwargs = _build_engine_kwargs(args)
        gpu = _gpu_info(engine_kwargs.get("tensor_parallel_size", 1))
        results, cancelled, duration, engine_stats = asyncio.run(
            run_replay(
                sessions, sampling=sampling, engine_kwargs=engine_kwargs,
                sec_multiplier=args.sec_multiplier, deadline_s=deadline,
            )
        )
    _summary(results)

    now = datetime.now()
    meta = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "host": socket.gethostname(),
        "vllm": vllm_ver,
        "commit": _git_commit(),
        "tag": args.tag,
        "model": {
            "name": name,
            "path": args.model,
            "config": str(_cfg_path(args)),
            "engine_args": engine_kwargs,
            "endpoint": args.endpoint,
        },
        "sampling": sampling,
        "workload": {
            "dataset": args.dataset,
            "split": args.split,
            "window": args.window or "all",
            "total_requests": n_req,
            "rate_per_min": args.rate,
            "sample": args.sample,
            "sec_multiplier": args.sec_multiplier,
            "tail_factor": args.tail_factor,
            "tail_grace_min": args.tail_grace,
            "deadline_s": deadline,
        },
        "gpu": gpu,
        "load": {"sessions": len(sessions), "turns": n_turns, "offered_turn_s": offered_turn_s},
        "cancelled_sessions": cancelled,
        "duration_s": duration,
    }
    win = (args.window or "all").replace(":", "-")
    load = f"_r{args.rate:g}" if args.rate else (f"_t{n_req}" if n_req else "")
    run_name = (
        f"{now.strftime('%Y-%m-%d_%H%M')}__{name}__{args.tag}__{args.split or 'data'}_{win}{load}"
    )
    runs_dir = Path(args.runs_dir)
    slo_specs = args.slo if args.slo is not None else _DEFAULT_SLO
    run_dir = runs_dir / run_name
    report.write_run(
        results, meta, run_dir, slo=report.parse_slo(slo_specs),
        engine_stats=engine_stats, cases=args.cases,
    )
    report.rebuild_index(runs_dir)
    print(f"记录写入 {run_dir}")


if __name__ == "__main__":  # pragma: no cover
    main()
