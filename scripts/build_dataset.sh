#!/usr/bin/env bash
# 生成可复现的评测数据集:由 Mooncake 重建会话与时序结构,填入真实多轮对话,
# 打包为 parquet 与 dataset card。参数透传给 build_dataset.py(--help 看全部)。
#
# 用法:
#   bash scripts/build_dataset.sh [--out-dir DIR] [--limit N] [--seed N]
#
# 依赖:datasets、tiktoken(pip install 'foretoken[server]')。
set -euo pipefail

python -m foretoken.data_prepare.build_dataset "$@"
