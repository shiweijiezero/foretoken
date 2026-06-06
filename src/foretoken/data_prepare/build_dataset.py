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

Foretoken 评测负载,覆盖 KV 管理与 MTP:由 Mooncake 重建会话与时序结构,填入真实多轮对话内容。

- 会话 / 时序 / 并发:来自 Mooncake trace(以 hash 去尾满块前缀重建会话,保留每轮真实 timestamp);
- 内容:来自 `{content}`(真实多轮对话),复用来自会话内累积(连贯,由内容决定、与缓存配置无关,
  不复刻 Mooncake 的 512 块量化);
- 回放:prompt 为文本,由 vLLM 自行 tokenize;输出长度交回放阶段(统一 max_tokens 上限 + 自然 EOS),不预设。

## split
按 Mooncake config 分场景:`{splits}`。各 split 保留各自真实的 0~1 小时时序,互不串联;按需分场景
回放(对话 / agent 各自的 KV/MTP 表现)或合并回放。

## 字段
- `timestamp_ms` — 真实到达时刻(供 foretoken `bench/replay.py` 回放)
- `prompt` — 累积的多轮真实文本

来源:Mooncake `{mooncake}`、内容 `{content}`。生成:`scripts/build_dataset.sh`。
"""


def build_hf_dataset(
    splits: dict[str, Iterable[dict]],
    out_dir: str,
    *,
    name: str,
    mooncake: str,
    content: str,
) -> str:
    """把各 config 的 rows 分别写成 parquet split(`<split>.parquet`)+ dataset card。需要 datasets。"""
    try:
        from datasets import Dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    os.makedirs(out_dir, exist_ok=True)
    for split, rows in splits.items():
        Dataset.from_list(list(rows)).to_parquet(os.path.join(out_dir, f"{split}.parquet"))
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(_CARD.format(name=name, mooncake=mooncake, content=content, splits=", ".join(splits)))
    return out_dir


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from foretoken.data_prepare.make_workload import _stream_hf, fill_sessions, reconstruct_sessions

    ap = argparse.ArgumentParser(description="使用 会话时间戳 + 真实对话 构建仿真数据集")
    ap.add_argument("--mooncake", default="valeriol29/mooncake-traces")
    ap.add_argument("--mooncake-config", default="conversation,mooncake,toolagent",
                    help="Mooncake config(逗号分隔多个;synthetic 无真多轮,默认不含)")
    ap.add_argument("--content", default="lightseekorg/kimi-mtp-dataset")
    ap.add_argument("--out-dir", default="data/foretoken-trace")
    ap.add_argument("--name", default="foretoken-stitched-trace")
    ap.add_argument("--limit", type=int, default=None, help="最多读取的 Mooncake 行数")
    ap.add_argument("--seed", type=int, default=0, help="对话打乱种子")
    args = ap.parse_args()

    # 各 config 各自自适应重建会话、各自填充,作为一个 split(保留各自真实 0~1h 时序,互不串联)
    splits: dict = {}
    for cfg in (c.strip() for c in args.mooncake_config.split(",") if c.strip()):
        sess_c = reconstruct_sessions(list(_stream_hf(args.mooncake, "train", args.limit, config=cfg)))
        rows_c = list(fill_sessions(sess_c, _stream_hf(args.content, "train", None), seed=args.seed))
        splits[cfg] = rows_c
        print(f"  {cfg}: {len(sess_c)} sessions, {len(rows_c)} rows")
    out = build_hf_dataset(
        splits, args.out_dir, name=args.name, mooncake=args.mooncake, content=args.content,
    )
    print(f"dataset -> {out}")
