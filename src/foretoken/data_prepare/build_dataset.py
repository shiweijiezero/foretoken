"""把缝合产物打包成可复现的 HF 数据集(parquet + dataset card),后续直接 `load_dataset` 用。

为什么:数据集一旦固定 → **可复现**(同一份,gate-zero 友好)+ **省时**(不每次现缝)+ **可开源**。
方法 = Mooncake 重建会话骨架(时序/并发/轮次)+ 真实多轮对话(连贯内容 + 会话内累积复用,**配置
无关**,不复刻 Mooncake 的 512 块量化,见 docs/07 §6.6)。
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

Foretoken 评测负载(一套全真 KV+MTP):**Mooncake 重建会话骨架 + 真实多轮对话**。

- **会话 / 时序 / 并发** 来自 Mooncake trace(用 hash 前缀链重建会话 + 每轮真实 timestamp);
- **内容** 来自 `{content}`(真实多轮对话),**复用 = 会话内累积**(连贯、**配置无关**,不复刻
  Mooncake 的 512 块量化);
- **tokenizer = `{tokenizer}`**(仅用于 token 估算;prompt 是文本,回放时 vLLM 自己 tokenize)。

## 字段
- `timestamp_ms` — 真实到达时刻(配 foretoken `bench/replay.py` 回放)
- `prompt` — 累积的多轮真实文本(连贯)
- `expected_output_len` — 该轮真实回复的 token 估算

来源:Mooncake `{mooncake}` · 内容 `{content}`。生成:`scripts/build_dataset.sh`。
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

    from foretoken.data_prepare.stitch import _stream_hf, fill_sessions, reconstruct_sessions

    ap = argparse.ArgumentParser(description="缝合(会话重建 + 真实对话)→ 可复现 HF 数据集")
    ap.add_argument("--mooncake", default="valeriol29/mooncake-traces")
    ap.add_argument("--content", default="lightseekorg/kimi-mtp-dataset")
    ap.add_argument("--tokenizer", required=True, help="GLM tokenizer HF id/路径(token 估算)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--name", default="foretoken-stitched-trace")
    ap.add_argument("--limit", type=int, default=None, help="最多读多少行 Mooncake")
    ap.add_argument("--seed", type=int, default=0, help="对话 shuffle 种子")
    args = ap.parse_args()

    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    # 1) Mooncake → 重建会话骨架(谁是同一会话第几轮 + 每轮 timestamp + 并发)
    sessions = reconstruct_sessions(_stream_hf(args.mooncake, "train", args.limit))
    print(f"reconstructed sessions = {len(sessions)}")
    # 2) 读够真实对话(筛掉短的后仍够),shuffle,按轮数塞 → 累积 prompt + Mooncake timestamp
    n_conv = max(10000, len(sessions) * 5)
    rows = fill_sessions(
        sessions,
        _stream_hf(args.content, "train", n_conv),
        len_fn=lambda s: len(tok.encode(s, add_special_tokens=False)),
        seed=args.seed,
    )
    out = build_hf_dataset(
        rows, args.out_dir, name=args.name, mooncake=args.mooncake,
        content=args.content, tokenizer=args.tokenizer,
    )
    print(f"dataset -> {out}")
