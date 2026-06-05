#!/usr/bin/env bash
# 生成可复现的 Foretoken 评测数据集(Mooncake 重建会话骨架 + 真实多轮对话内容)。
# 库逻辑在 src/foretoken/data_prepare/{stitch,build_dataset}.py;本脚本只是操作入口(薄壳)。
# 需要 datasets + transformers + GLM tokenizer(foretoken[server])。
set -euo pipefail

: "${TOKENIZER:?need TOKENIZER (GLM tokenizer HF id/路径,如 GLM-4.5-Air)}"
OUT_DIR="${OUT_DIR:-data/foretoken-trace}"
LIMIT="${LIMIT:-5000}"   # 取多少行 Mooncake(唯一 hash 数会决定要读多少 kimi 块,自动对齐)

python -m foretoken.data_prepare.build_dataset \
  --tokenizer "${TOKENIZER}" \
  --out-dir "${OUT_DIR}" \
  --limit "${LIMIT}"

# 产出:${OUT_DIR}/{data.parquet, README.md(dataset card)}
# 后续:load_dataset("parquet", data_files="${OUT_DIR}/data.parquet") → 喂 bench/replay.py
