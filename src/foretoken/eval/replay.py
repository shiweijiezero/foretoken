"""Mooncake trace 回放 + 缝合法(docs/07 §6.5)。

缝合:Mooncake 当骨架(时序 / 并发 / 轮次 / 长度),prompt pool 当血肉(真实内容),
真实模型现场生成 → 一套负载同时压 KV(时序 + 复用)和 MTP(真实内容)。

⚠️ 单独跑 benchmark 时 KV/MTP 分工不同(docs/10):AIME/GPQA/LCB 压 MTP、SWE-bench 压 KV;
"同时压"靠的正是缝合。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TraceEntry:
    """Mooncake trace 一行(骨架)。"""

    timestamp_ms: int
    input_length: int
    output_length: int
    hash_ids: list[int] = field(default_factory=list)  # 512-token block ids(前缀复用结构)


@dataclass
class StitchedRequest:
    timestamp_ms: int
    prompt: str           # 来自 prompt pool(真实内容)
    max_tokens: int       # 来自 trace 的 output_length
    hash_ids: list[int]   # 保留骨架的复用结构(供门槛零核对)


def load_mooncake_trace(path: str) -> list[TraceEntry]:
    """读 Mooncake jsonl trace。

    TODO(P0):字段映射(timestamp / input_length / output_length / hash_ids)+ 块大小
    对齐 512(docs/07 line 145)。
    """
    raise NotImplementedError("TODO: 解析 Mooncake jsonl。")


def stitch(trace: list[TraceEntry], prompt_pool: list[str]) -> list[StitchedRequest]:
    """把 trace 骨架与 prompt pool 内容缝合。

    TODO(P0):长度匹配策略(按 input_length 选 / 截 prompt),保留 hash_ids 复用结构。
    """
    raise NotImplementedError("TODO: 缝合骨架(时序 / 长度 / 复用)+ 血肉(prompt pool)。")


def replay(requests: list[StitchedRequest], base_url: str, model: str) -> None:
    """把缝合后的请求按时间戳回放到 OpenAI 兼容端点(vllm serve)。

    TODO(P0):异步按 timestamp 发请求、采集 TTFT/TPOT、产出 goodput.RequestRecord。
    优先复用 ``vllm bench serve --dataset-name custom``(见 scripts/run_baseline.sh),
    自研回放只在需要精确时间戳骨架时启用。
    """
    raise NotImplementedError("TODO: 按时间戳异步回放 + 采集 TTFT/TPOT。")
