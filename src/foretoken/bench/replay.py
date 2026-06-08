"""闭环会话回放:进程内自起 vLLM 引擎(AsyncLLM),按会话维护上下文、模型现场生成回复拼下一轮,
采集 TTFT/TPOT/E2E、算 goodput。

引擎随本进程生命周期:进程退出 / engine.shutdown() 即释放 GPU,无独立 HTTP server、无孤儿占卡。
时间调度(每会话):首轮按绝对 timestamp;之后上一轮按时完成(C ≤ T_k)则按绝对 T_k,超时则
C + 原间隔(T_k − T_{k-1})。跨会话并发、会话内串行。

纯函数(next_send_ms / group_sessions / parse_window / deadline_seconds / goodput_*)顶层无 vllm
依赖,可本地单测;引擎相关(_gen_once / replay / main)延迟导入 vllm。
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from foretoken.bench import report
from foretoken.bench.model_params import read, resolve

# 注意:vllm / datasets / torch / matplotlib 仅服务器装(本地无 vLLM wheel),故在用到的函数内延迟导入,
# 以保本模块纯函数层可在本地无重依赖单测(见模块 docstring)。


@dataclass
class TurnResult:
    session_id: int
    turn: int
    ttft_ms: float
    tpot_ms: float
    e2e_ms: float
    output_tokens: int
    text: str = ""  # 现场生成的回复(供无损校验 / 对照)
    ok: bool = True


def next_send_ms(turn_idx: int, t_cur: int, t_prev: int, complete_prev_ms: float, t0: int) -> float:
    """该轮发出的相对(对 t0)时刻 ms。首轮 = 绝对;上一轮按时完成 → 绝对,超时 → 完成 + 原间隔。"""
    t_cur_rel = t_cur - t0
    if turn_idx == 0 or complete_prev_ms <= t_cur_rel:
        return float(t_cur_rel)
    return complete_prev_ms + (t_cur - t_prev)  # 超时:完成时刻 + 原本的思考间隔


def group_sessions(
    rows: Iterable[dict], *, window: tuple[int, int] | None = None
) -> dict[int, list[dict]]:
    """按 session_id 分组(组内按 turn 排序);window=(a_ms,b_ms) 按真实 ts 截取轮次。

    会话首轮 ts 须落 [a,b] 才纳入(准入);后续轮真实 ts > b 的**截断**、不回放(只留窗内轮)。
    背压(decoding 慢导致实际发出晚)不在此截——那些轮真实 ts 在窗内,回放时自然拖、跑完。
    """
    sess: dict[int, list[dict]] = {}
    for r in rows:
        sess.setdefault(r["session_id"], []).append(r)
    for s in sess.values():
        s.sort(key=lambda r: r["turn"])
    if window is None:
        return sess
    t0 = min(r["timestamp_ms"] for s in sess.values() for r in s)
    a, b = window
    out: dict[int, list[dict]] = {}
    for k, s in sess.items():
        if not (a <= s[0]["timestamp_ms"] - t0 <= b):  # 首轮不在窗内 → 整会话不纳入
            continue
        out[k] = [r for r in s if r["timestamp_ms"] - t0 <= b]  # 截断真实 ts 超 b 的后续轮
    return out


def sample_sessions(
    sessions: dict[int, list[dict]],
    *,
    n_requests: int | None = None,
    fraction: float | None = None,
    seed: int = 0,
) -> dict[int, list[dict]]:
    """会话级下采样,**整会话保留**(多轮完整,绝不拆会话);把集群级 trace 负载匹配单实例。

    二选一:`n_requests` 随机选整会话直到累计轮(request)数达目标(整会话保留,末个会话可能略超);
    `fraction` 留该比例会话。都不给 → 原样。真实时间戳 / 突发形态不变,可复现(seed)。
    """
    if n_requests is None and fraction is None:
        return sessions
    sids = sorted(sessions)
    random.Random(seed).shuffle(sids)  # 可复现的随机序
    if n_requests is not None:
        kept: list[int] = []
        total = 0
        for sid in sids:
            if total >= n_requests:
                break
            kept.append(sid)
            total += len(sessions[sid])
        keep = set(kept)
    else:
        if not 0.0 < fraction <= 1.0:
            raise ValueError("fraction must be in (0, 1]")
        keep = set(sids[: max(1, round(len(sids) * fraction))])
    return {sid: s for sid, s in sessions.items() if sid in keep}  # 保原 session 顺序


def load_rows(dataset: str, split: str | None = None) -> list[dict]:
    """加载回放数据行(行 schema:session_id/turn/timestamp_ms/user;可选 assistant/system)。

    `dataset` 可为:本地 `.jsonl`/`.json`/`.parquet`/`.csv` 文件、本地 HF 数据集目录,或 HF hub id
    (配 `split`=配置名,如 conversation/mooncake/toolagent)。不绑定单一数据源,便于自带 trace。
    """
    from datasets import load_dataset  # 仅服务器装(见模块顶部说明)

    p = Path(dataset)
    if p.is_file():  # 本地文件:按扩展名选 builder
        ext = p.suffix.lstrip(".")
        fmt = {"jsonl": "json", "json": "json", "parquet": "parquet", "csv": "csv"}.get(ext, ext)
        return list(load_dataset(fmt, data_files=str(p), split="train"))
    if p.is_dir():  # 本地 HF 数据集目录
        return list(load_dataset(str(p), name=split, split="train"))
    return list(load_dataset(dataset, split, split="train"))  # HF hub id;split=配置名


def parse_window(spec: str | None) -> tuple[int, int] | None:
    """'N' → (0, N 分);'A:B' → (A 分, B 分);None → 全量。单位 ms。"""
    if not spec:
        return None
    if ":" in spec:
        a, b = spec.split(":", 1)
        return (int(float(a) * 60_000), int(float(b) * 60_000))
    return (0, int(float(spec) * 60_000))


def deadline_seconds(
    window: tuple[int, int] | None, sec_multiplier: float, tail_factor: float
) -> float | None:
    """回放墙钟上限 s = 窗口跨度 × sec_multiplier × tail_factor;到点取消在飞请求。

    掐长尾:个别会话(高温采样易陷退化循环)会一路顶到 max_tokens、独占引擎拖垮整轮。
    无窗口或 tail_factor<=0 → None(不设限,跑到全部完成)。
    """
    if not window or tail_factor <= 0:
        return None
    return (window[1] - window[0]) / 1000.0 * sec_multiplier * tail_factor


def goodput_per_gpu_byte_second(
    results: Iterable[TurnResult],
    *,
    ttft_ms: float,
    tpot_ms: float,
    duration_s: float,
    gpu_bytes: float,
) -> float:
    """满足 SLO(TTFT 且 TPOT)的有效 output token / (时长 × GPU 字节)——统一标尺。"""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if gpu_bytes <= 0:
        raise ValueError("gpu_bytes must be > 0")
    good = sum(
        r.output_tokens for r in results if r.ok and r.ttft_ms <= ttft_ms and r.tpot_ms <= tpot_ms
    )
    return good / (duration_s * gpu_bytes)


async def _gen_once(engine, tokenizer, messages, sampling_params, request_id):
    """进程内流式生成 → (text, ttft_ms, tpot_ms, e2e_ms, n_tokens, ok)。

    apply_chat_template 把对话拼成 prompt;engine.generate 流式产出累积 RequestOutput,
    首个含 token 的产出计 TTFT,末次的 token 数计长度。被取消时 abort 该请求以释放引擎槽位。
    """
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    start = time.perf_counter()
    first: float | None = None
    n = 0
    text = ""
    try:
        async for out in engine.generate(prompt, sampling_params, request_id):
            co = out.outputs[0]
            if first is None and co.token_ids:
                first = time.perf_counter()
            n = len(co.token_ids)
            text = co.text
    except asyncio.CancelledError:
        await engine.abort(request_id)  # 取消:回收引擎中的在飞请求
        raise
    except Exception:  # noqa: BLE001  单条失败不中断整轮回放
        return "", 0.0, 0.0, 0.0, 0, False
    if first is None:
        return "", 0.0, 0.0, 0.0, 0, False
    end = time.perf_counter()
    ttft = (first - start) * 1000.0
    tpot = ((end - first) * 1000.0 / (n - 1)) if n > 1 else 0.0
    return text, ttft, tpot, (end - start) * 1000.0, n, True


async def replay(
    sessions: dict[int, list[dict]],
    engine,
    tokenizer,
    *,
    sampling_params,
    sec_multiplier: float = 1.0,
    deadline_s: float | None = None,
) -> tuple[list[TurnResult], int]:
    """闭环回放:跨会话并发、会话内串行(现场生成回复 + 混合时间调度)。返回 (已完成轮, 取消会话数)。

    engine/tokenizer:进程内 vLLM 引擎与其分词器(见 main)。sampling_params 逐请求一致以可复现。
    deadline_s:墙钟上限(见 deadline_seconds);到点取消在飞会话,只保留已完成轮。
    """
    t0 = min(s[0]["timestamp_ms"] for s in sessions.values())
    results: list[TurnResult] = []
    start = time.perf_counter()

    async def run_session(turns: list[dict]) -> None:
        sid = turns[0]["session_id"]
        system = turns[0].get("system")
        messages = [{"role": "system", "content": system}] if system else []
        complete_prev = 0.0
        t_prev = turns[0]["timestamp_ms"]
        for k, turn in enumerate(turns):
            send_rel = next_send_ms(k, turn["timestamp_ms"], t_prev, complete_prev, t0)
            now_ms = (time.perf_counter() - start) * 1000.0
            await asyncio.sleep(max(0.0, (send_rel * sec_multiplier - now_ms) / 1000.0))
            messages.append({"role": "user", "content": turn["user"]})
            text, ttft, tpot, e2e, n, ok = await _gen_once(
                engine, tokenizer, messages, sampling_params, f"{sid}-{k}"
            )
            complete_prev = (time.perf_counter() - start) * 1000.0  # C_k 相对完成时刻
            messages.append({"role": "assistant", "content": text})  # 现场回复接回历史
            results.append(TurnResult(sid, k, ttft, tpot, e2e, n, text, ok))
            t_prev = turn["timestamp_ms"]

    tasks = [asyncio.create_task(run_session(s)) for s in sessions.values()]
    if not tasks:
        return results, 0
    _done, pending = await asyncio.wait(tasks, timeout=deadline_s)
    cancelled = len(pending)
    if pending:  # 到点未完成的会话:取消并回收(只保留已记录的完成轮)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # 等取消落定
        print(f"达回放时限 {deadline_s:.0f}s:取消 {cancelled} 个未完成会话")
    return results, cancelled


def _summary(results: list[TurnResult]) -> None:
    """打印 TTFT/TPOT/E2E 分位 + 完成数(简版;详细汇总 / 图见展示脚本)。"""
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


def _cfg_name(args) -> str:
    """报告 / 目录用逻辑名:有 --config 用其文件名 stem,否则取 --model 的 basename。"""
    return Path(args.config).stem if args.config else Path(args.model).name


def _cfg_path(args):
    """配置文件路径:--config 直接指定的文件,缺省 default.toml(见 model_params)。"""
    return resolve(args.config)


def _build_sampling(args) -> dict:
    """组装采样:config 官方值 → --param 透传/覆盖任意 vLLM 采样参数 → 固定 seed。"""
    sampling = read(_cfg_path(args))  # 配置文件的 [sampling](官方采样)
    for kv in args.param:  # 覆盖 / 透传任意 vLLM 采样参数(K=V,值按 JSON 解析)
        k, _, v = kv.partition("=")
        try:
            sampling[k] = json.loads(v)
        except json.JSONDecodeError:
            sampling[k] = v
    sampling["seed"] = args.seed
    return sampling


def _build_engine_kwargs(args) -> dict:
    """组装引擎参数:config [serve] → --engine-param 透传/覆盖任意 AsyncEngineArgs 字段。"""
    serve = read(_cfg_path(args), "serve")  # 配置文件的 [serve](默认即配置 A)
    serve["model"] = args.model
    serve.setdefault("seed", args.seed)
    for kv in args.engine_param:  # 任意 AsyncEngineArgs 字段(K=V,值按 JSON 解析)
        k, _, v = kv.partition("=")
        try:
            serve[k] = json.loads(v)
        except json.JSONDecodeError:
            serve[k] = v
    return serve


async def _run(args, sessions, deadline, sampling, engine_kwargs):
    """进程内起引擎 → 回放 → finally 关停(释放 GPU)。返回 (results, cancelled, duration_s)。"""
    from vllm import SamplingParams
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.v1.engine.async_llm import AsyncLLM

    engine = AsyncLLM.from_engine_args(AsyncEngineArgs(**engine_kwargs))
    try:
        tokenizer = engine.get_tokenizer()
        sampling_params = SamplingParams(**sampling)
        t = time.perf_counter()
        results, cancelled = await replay(
            sessions,
            engine,
            tokenizer,
            sampling_params=sampling_params,
            sec_multiplier=args.sec_multiplier,
            deadline_s=deadline,
        )
        return results, cancelled, time.perf_counter() - t
    finally:
        engine.shutdown()  # 关停引擎、释放 GPU(正常 / 报错 / 中断都走)


def _gpu_info(hint: int) -> dict:
    """主进程查可见 GPU 数 / 单卡显存(归一化 goodput 用);失败回退 hint(=TP)。"""
    try:
        import torch

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
    except Exception:  # noqa: BLE001  服务器经 rsync 同步、无 .git
        return "unknown"


if __name__ == "__main__":  # pragma: no cover
    import argparse

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
        help="HF 数据集 id,或本地 .jsonl/.parquet 文件 / 目录(schema 见 load_rows)",
    )
    ap.add_argument(
        "--split",
        default=None,
        help="HF 数据集配置名(conversation/mooncake/toolagent);本地文件可省",
    )
    ap.add_argument("--window", default=None, help="时间窗(分钟):N 或 A:B")
    ap.add_argument(
        "--n-requests",
        type=int,
        default=None,
        help="目标 request(轮)数:会话级下采样到此量,匹配单实例硬件(整会话保留)",
    )
    ap.add_argument(
        "--sample",
        type=float,
        default=None,
        help="会话级下采样比例 (0,1](与 --n-requests 二选一)",
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
        help="回放墙钟上限 = 窗口跨度 × sec_multiplier × 此值;到点取消在飞请求(掐长尾)。<=0 不设限",
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
        "--tag",
        default="vllm-default",
        help="优化变体标签(自描述,排行榜区分):vllm-default(stock 基线)/ kv-aware / mtp / kv+mtp",
    )
    ap.add_argument("--runs-dir", default="runs", help="实验记录根目录(默认 runs/)")
    args = ap.parse_args()

    import vllm  # 仅服务器装(见模块顶部说明)

    rows = load_rows(args.dataset, args.split)
    window = parse_window(args.window)
    sessions = group_sessions(rows, window=window)
    sessions = sample_sessions(  # 会话级下采样匹配硬件
        sessions, n_requests=args.n_requests, fraction=args.sample, seed=args.seed
    )
    deadline = deadline_seconds(window, args.sec_multiplier, args.tail_factor)
    n_turns = sum(len(s) for s in sessions.values())
    span_s = (window[1] - window[0]) / 1000.0 if window else None
    offered_turn_s = (n_turns / span_s) if span_s else None  # 提供负载(轮/s),扫描曲线 x 轴
    dl = f"{deadline:.0f}s" if deadline else "无"
    sampling = _build_sampling(args)
    engine_kwargs = _build_engine_kwargs(args)
    name = _cfg_name(args)
    print(
        f"{name}: {len(sessions)} 会话, {n_turns} 轮 | "
        f"n_requests={args.n_requests} sample={args.sample} | 时限 {dl} | 采样 {sampling}"
    )

    results, cancelled, duration = asyncio.run(
        _run(args, sessions, deadline, sampling, engine_kwargs)
    )
    _summary(results)

    now = datetime.now()
    meta = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "host": socket.gethostname(),
        "vllm": vllm.__version__,
        "commit": _git_commit(),
        "tag": args.tag,
        "model": {
            "name": name,
            "path": args.model,
            "config": str(_cfg_path(args)),
            "engine_args": engine_kwargs,
        },
        "sampling": sampling,
        "workload": {
            "dataset": args.dataset,
            "split": args.split,
            "window": args.window or "all",
            "n_requests": args.n_requests,
            "sample": args.sample,
            "sec_multiplier": args.sec_multiplier,
            "tail_factor": args.tail_factor,
            "deadline_s": deadline,
        },
        "gpu": _gpu_info(engine_kwargs.get("tensor_parallel_size", 1)),
        "load": {"sessions": len(sessions), "turns": n_turns, "offered_turn_s": offered_turn_s},
        "cancelled_sessions": cancelled,
        "duration_s": duration,
    }
    win = (args.window or "all").replace(":", "-")
    run_name = f"{now.strftime('%Y-%m-%d_%H%M')}__{name}__{args.tag}__{args.split or 'data'}_{win}"
    runs_dir = Path(args.runs_dir)
    run = report.write_run(results, meta, runs_dir / run_name)
    report.rebuild_index(runs_dir)
    print(f"记录写入 {runs_dir / run_name}")
