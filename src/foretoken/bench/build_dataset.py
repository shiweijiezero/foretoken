"""把模式 B 缝合产物打包成可复现的 HF 数据集(parquet + dataset card),后续直接 `load_dataset` 用。

为什么:数据集 = f(Mooncake trace, kimi 内容, GLM tokenizer) 固定后 → **可复现**(同一份,gate-zero
友好)+ **省时**(不每次现缝)+ **可开源**。**绑定 GLM tokenizer**(`prompt_token_ids` 是 GLM vocab、
512 块对齐 GLM token)→ 换模型须重建。**让步(模式 B)**:跨块边界偶不连贯 → KV 复用 100% 真、
MTP 接受率大体真(边界噪声)。
"""
from __future__ import annotations

import os
from collections.abc import Iterable

_CARD = """\
---
license: apache-2.0
task_categories: [text-generation]
tags: [llm-serving, kv-cache, mtp, benchmark, mooncake, foretoken]
---

# {name}

Foretoken 评测负载(模式 B「含多用户并发的一套全真」):**Mooncake 真实并发骨架 + 真实文本块**。

- **复用结构 100% 来自 Mooncake `hash_ids`**(相同 hash → 同一真实文本块);并发 / 到达 / 复用逐条真实。
- **内容** = `{content}`(真实多轮对话);**tokenizer = `{tokenizer}`**(绑定;换模型须重建)。
- **让步**:跨块边界偶不连贯 → **KV 复用真实、MTP 接受率大体真**(边界噪声)。

## 字段
- `timestamp_ms` — 真实到达时刻(配 foretoken `bench/replay.py` 按时间戳回放)
- `prompt_token_ids` — GLM token(**复用边界精确**,回放优先用它)
- `prompt` — 解码文本(备查 / 通用 endpoint)
- `expected_output_len` / `hash_ids` — 供 gate-zero 核对

来源:Mooncake `{mooncake}` · 内容 `{content}`。生成:`python -m foretoken.bench.build_dataset`。
"""


def build_hf_dataset(
    rows: Iterable[dict],
    out_dir: str,
    *,
    name: str,
    mooncake: str,
    content: str,
    tokenizer: str,
) -> str:
    """把缝合 rows 存成 parquet + 写 dataset card 到 out_dir;返回 out_dir。需要 datasets。"""
    try:
        from datasets import Dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    os.makedirs(out_dir, exist_ok=True)
    Dataset.from_list(list(rows)).to_parquet(os.path.join(out_dir, "data.parquet"))
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(_CARD.format(name=name, mooncake=mooncake, content=content, tokenizer=tokenizer))
    return out_dir


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from foretoken.bench.stitch import (
        _stream_hf,
        build_block_pool,
        extract_texts,
        fill_mooncake_trace,
    )

    ap = argparse.ArgumentParser(description="缝合产物 → 可复现 HF 数据集(parquet + card)")
    ap.add_argument("--mooncake", default="valeriol29/mooncake-traces")
    ap.add_argument("--content", default="lightseekorg/kimi-mtp-dataset")
    ap.add_argument("--tokenizer", required=True, help="GLM tokenizer HF id/路径")
    ap.add_argument("--out-dir", required=True, help="数据集输出目录(parquet + README)")
    ap.add_argument("--name", default="foretoken-stitched-trace")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--content-limit", type=int, default=2000)
    args = ap.parse_args()

    from transformers import AutoTokenizer

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
    out = build_hf_dataset(
        rows, args.out_dir, name=args.name, mooncake=args.mooncake,
        content=args.content, tokenizer=args.tokenizer,
    )
    print(f"dataset -> {out}  (load: load_dataset('parquet', data_files='{out}/data.parquet'))")
