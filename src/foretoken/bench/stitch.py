"""缝合器:把真实多轮对话(如 lightseekorg/kimi-mtp-dataset)按"多轮累积"展开成评测负载。

**复用结构来自真实多轮累积**(不是硬套 Mooncake 的 hash):
    turn k 的 prompt = system + 前 k-1 轮(user/assistant)+ 第 k 轮 user
→ turn k 的 prompt **严格以 turn k-1 的 prompt 为前缀** = 真实 chat/agent 的 KV 复用来源。

输出 **superset jsonl**(`prompt` + `session_id` + `turn` + `expected_output_len` + 可选 `timestamp_ms`):
- **A 路径**(现成):`vllm bench serve --dataset-name custom`,只用 `prompt`,到达用 `--request-rate`/`--burstiness` 合成。
- **B 路径**(将来):同一份数据 + 自写回放器读 `timestamp_ms` 复现真实到达。

纯数据处理,不依赖 GPU / vLLM,本地可跑可测。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass


@dataclass
class StitchedRequest:
    session_id: str
    turn: int
    prompt: str
    expected_output_len: int
    timestamp_ms: int | None = None  # B 路径(自写回放)用;A 路径(vLLM custom)忽略


def _approx_token_len(text: str) -> int:
    """粗估 token 数(~4 char/token);落地评测用真 tokenizer 更准。"""
    return max(1, len(text) // 4)


def expand_conversation(
    conversations: list[dict],
    session_id: str,
    *,
    len_fn: Callable[[str], int] = _approx_token_len,
    sep: str = "\n\n",
) -> list[StitchedRequest]:
    """把一段多轮对话(ShareGPT 格式 {from, value})按累积前缀展开成多条请求(每个 user 轮一条)。

    非纯文本(多模态:value 不是 str)整条跳过,返回 []。
    """
    prefix: list[str] = []
    pending_human: str | None = None
    requests: list[StitchedRequest] = []
    turn = 0
    for msg in conversations:
        role = msg.get("from")
        val = msg.get("value")
        if not isinstance(val, str):
            return []  # 多模态 / 非文本对话 → 跳过整条
        if role == "system":
            prefix.append(f"system: {val}")
        elif role == "human":
            pending_human = val
        elif role == "gpt":
            if pending_human is None:
                continue  # 不规则(无配对 user),跳过
            turn += 1
            prompt = sep.join([*prefix, f"user: {pending_human}"])
            requests.append(
                StitchedRequest(
                    session_id=session_id,
                    turn=turn,
                    prompt=prompt,
                    expected_output_len=len_fn(val),
                )
            )
            prefix.append(f"user: {pending_human}")
            prefix.append(f"assistant: {val}")
            pending_human = None
    return requests


def stitch_dataset(
    records: Iterable[dict],
    *,
    len_fn: Callable[[str], int] = _approx_token_len,
    session_prefix: str = "s",
) -> Iterator[StitchedRequest]:
    """对一批对话记录(每条含 "conversations")逐条展开,session_id 自动编号。"""
    for i, rec in enumerate(records):
        yield from expand_conversation(
            rec.get("conversations", []), f"{session_prefix}{i}", len_fn=len_fn
        )


def write_jsonl(requests: Iterable[StitchedRequest], path: str) -> int:
    """写成 vLLM custom 可吃的 jsonl(每行含 prompt + 元数据);返回写出条数。"""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def _read_kimi(source: str, limit: int | None = None) -> Iterator[dict]:
    """流式读 kimi-mtp-dataset(HF id 或本地 parquet)。需要 `datasets` 库。"""
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 `datasets`:pip install datasets") from e
    ds = load_dataset(source, split="train", streaming=True)
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        yield row


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="缝合真实多轮对话 → vLLM custom jsonl")
    ap.add_argument("--source", default="lightseekorg/kimi-mtp-dataset", help="HF id 或本地 parquet")
    ap.add_argument("--out", required=True, help="输出 jsonl 路径")
    ap.add_argument("--limit", type=int, default=None, help="最多读多少条对话")
    args = ap.parse_args()
    count = write_jsonl(stitch_dataset(_read_kimi(args.source, args.limit)), args.out)
    print(f"wrote {count} requests -> {args.out}")
