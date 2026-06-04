"""缝合器(主线 = 模式 B):Mooncake 真实并发骨架 + 真实文本块 → "含多用户并发的一套全真"负载。

给 Mooncake trace 的每个 hash_id 绑定一段固定真实文本块(**相同 hash → 同一真实块**)→ Mooncake
的块级前缀复用 100% 映射成真实文本前缀复用;并发 / 到达 / 复用逐条来自 Mooncake(只有真实生产
trace 才有的多用户并发),内容来自真实多轮对话(`lightseekorg/kimi-mtp-dataset`)。

产出带 `timestamp_ms` 的 trace → 配 `bench/replay.py` 按真实时刻回放(B 路径)= KV + MTP 一套通吃。

**为什么没有 A 路径**:A 路径(对话累积 → custom + `--request-rate` 合成到达)**没有真实多用户
并发**(docs/07 §6.6),是退化版;而纯 MTP benchmark(AIME/sharegpt,不在乎到达)直接用
`vllm bench serve --dataset-name hf/sharegpt + --request-rate`(vLLM 原生、零自写)。两头都不需要 A。

纯数据处理(切块 / 映射),tokenizer 注入;本地可跑可测(真跑 main 需 datasets + GLM tokenizer)。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator


def extract_texts(records: Iterable[dict]) -> Iterator[str]:
    """从对话记录提取真实文本片段(human/gpt 的 value),作块池原料(跳过多模态非 str)。"""
    for rec in records:
        for msg in rec.get("conversations", []):
            v = msg.get("value")
            if isinstance(v, str) and v:
                yield v


def build_block_pool(
    texts: Iterable[str],
    encode: Callable[[str], list[int]],
    block_size: int = 512,
) -> list[list[int]]:
    """把真实文本拼接、token 化、切成 block_size-token 的真实 token 块池(供 hash 映射)。"""
    ids: list[int] = []
    for t in texts:
        ids.extend(encode(t))
    return [ids[i : i + block_size] for i in range(0, len(ids), block_size) if ids[i : i + block_size]]


def fill_mooncake_trace(
    trace_rows: Iterable[dict],
    block_pool: list[list[int]],
    decode: Callable[[list[int]], str],
    *,
    label_hash_ids: str = "hash_ids",
    label_timestamp: str = "timestamp",
    label_input_length: str = "input_length",
    label_output_length: str = "output_length",
) -> Iterator[dict]:
    """给 Mooncake trace 的每个 hash_id 绑定固定真实文本块 → 带真实内容 + 真实时序的请求。

    **相同 hash_id 永远映到同一真实块** → Mooncake 块级前缀复用 100% 映射成真实文本前缀复用。
    并发 / 到达 / 复用全保留(Mooncake),内容真实(块池),截到 input_length。
    """
    if not block_pool:
        raise ValueError("block_pool 为空;先用 build_block_pool 从真实文本造块池")
    hash_to_block: dict[int, int] = {}
    nxt = 0
    for row in trace_rows:
        token_ids: list[int] = []
        for h in row.get(label_hash_ids, []):
            if h not in hash_to_block:
                hash_to_block[h] = nxt % len(block_pool)  # 顺序取池 → 同请求的块大体连续、较连贯
                nxt += 1
            token_ids.extend(block_pool[hash_to_block[h]])
        input_length = int(row.get(label_input_length, len(token_ids)))
        token_ids = token_ids[:input_length]
        yield {
            "timestamp_ms": int(row[label_timestamp]),
            "prompt": decode(token_ids),
            "expected_output_len": int(row.get(label_output_length, 0)),
            "hash_ids": list(row.get(label_hash_ids, [])),
        }


def write_trace_jsonl(rows: Iterable[dict], path: str) -> int:
    """写带 timestamp 的 trace jsonl(给 bench/replay.py 回放);返回条数。"""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _stream_hf(source: str, split: str, limit: int | None) -> Iterator[dict]:
    """流式读 HF 数据集(Mooncake trace / 对话内容)。需要 datasets。"""
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    ds = load_dataset(source, split=split, streaming=True)
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        yield row


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(
        description="模式B缝合:Mooncake 并发骨架 + 真实文本块 → 带 timestamp 的 trace(配 replay.py)"
    )
    ap.add_argument("--mooncake", default="valeriol29/mooncake-traces", help="Mooncake trace HF id/路径")
    ap.add_argument("--content", default="lightseekorg/kimi-mtp-dataset", help="真实多轮内容 HF id/路径")
    ap.add_argument("--tokenizer", required=True, help="GLM tokenizer(HF id/路径):切 512 块 + 解码")
    ap.add_argument("--out", required=True, help="输出带 timestamp 的 trace jsonl")
    ap.add_argument("--limit", type=int, default=None, help="最多读多少行 Mooncake")
    ap.add_argument("--content-limit", type=int, default=2000, help="读多少条对话造块池")
    args = ap.parse_args()

    from transformers import AutoTokenizer  # 需 transformers(server extra)

    tok = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    pool = build_block_pool(
        extract_texts(_stream_hf(args.content, "train", args.content_limit)),
        encode=lambda s: tok.encode(s, add_special_tokens=False),
    )
    rows = fill_mooncake_trace(
        _stream_hf(args.mooncake, "train", args.limit),
        pool,
        decode=lambda ids: tok.decode(ids),
    )
    count = write_trace_jsonl(rows, args.out)
    print(f"wrote {count} requests -> {args.out}")
