"""把缝合产物打包为可复现的 HF 数据集(parquet + dataset card),供 load_dataset 直接加载。

固定数据集的好处:可复现(同一份,利于回放保真校验)、省时(无需每次重新缝合)、可开源。方法:由
Mooncake 重建会话与时序结构(时序 / 并发 / 轮次),填入真实多轮对话内容(连贯,会话内累积复用,由
内容决定、配置无关,不复刻 Mooncake 的 512 块量化;见 docs/07 §6.6)。
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

Foretoken 评测负载,在同一份数据上覆盖 KV 管理与 MTP:由 Mooncake 重建会话与时序结构,填入真实
多轮对话内容。

- 会话 / 时序 / 并发:来自 Mooncake trace(以 hash 前缀链重建会话,保留每轮真实 timestamp);
- 内容:来自 `{content}`(真实多轮对话),复用来自会话内累积(连贯,由内容决定、与缓存配置无关,
  不复刻 Mooncake 的 512 块量化);
- 输出长度:由 tiktoken(o200k_base)估算;prompt 为文本,回放时由 vLLM 自行 tokenize。

## 字段
- `timestamp_ms` — 真实到达时刻(供 foretoken `bench/replay.py` 回放)
- `prompt` — 累积的多轮真实文本
- `expected_output_len` — 该轮真实回复的 token 估算

来源:Mooncake `{mooncake}`、内容 `{content}`。生成:`python -m foretoken.data_prepare.build_dataset`。
"""


def build_hf_dataset(
    rows: Iterable[dict],
    out_dir: str,
    *,
    name: str,
    mooncake: str,
    content: str,
) -> str:
    """把缝合 rows 写成 parquet 与 dataset card 到 out_dir,返回 out_dir。需要 datasets。"""
    try:
        from datasets import Dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    os.makedirs(out_dir, exist_ok=True)
    Dataset.from_list(list(rows)).to_parquet(os.path.join(out_dir, "data.parquet"))
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(_CARD.format(name=name, mooncake=mooncake, content=content))
    return out_dir


if __name__ == "__main__":  # pragma: no cover
    import argparse

    import tiktoken

    from foretoken.data_prepare.stitch import _stream_hf, fill_sessions, reconstruct_sessions

    ap = argparse.ArgumentParser(description="缝合(会话重建 + 真实对话)→ 可复现 HF 数据集")
    ap.add_argument("--mooncake", default="valeriol29/mooncake-traces")
    ap.add_argument("--content", default="lightseekorg/kimi-mtp-dataset")
    ap.add_argument("--out-dir", default="data/foretoken-trace")
    ap.add_argument("--name", default="foretoken-stitched-trace")
    ap.add_argument("--limit", type=int, default=None, help="最多读取的 Mooncake 行数")
    ap.add_argument("--seed", type=int, default=0, help="对话打乱种子")
    args = ap.parse_args()

    enc = tiktoken.get_encoding("o200k_base")  # 多语言,仅用于估算输出长度
    # 1) 由 Mooncake 重建会话与时序结构(同一会话的轮次顺序、每轮 timestamp、并发)
    sessions = reconstruct_sessions(_stream_hf(args.mooncake, "train", args.limit))
    print(f"reconstructed sessions = {len(sessions)}")
    # 2) 读取足量真实对话(滤除过短后仍充足),打乱后按轮数填入,得到累积 prompt 与 Mooncake timestamp
    n_conv = max(10000, len(sessions) * 5)
    rows = fill_sessions(
        sessions,
        _stream_hf(args.content, "train", n_conv),
        len_fn=lambda s: len(enc.encode(s)),
        seed=args.seed,
    )
    out = build_hf_dataset(
        rows, args.out_dir, name=args.name, mooncake=args.mooncake, content=args.content,
    )
    print(f"dataset -> {out}")
