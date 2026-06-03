#!/usr/bin/env bash
# 起 GLM-4.5-Air OpenAI 兼容服务(P0 真实推理基线)。跑在服务器 A100(8x80GB)。
#
# ⚠️ A100 = sm80,无原生 FP8(docs/09):GLM-4.5-Air 必须 BF16 权重,别拉 -FP8 变体
#    (Ampere 上 MoE-FP8 会报错)。
# ⚠️ 路径一律用环境变量,勿把内网路径写进本脚本(infra/servers.md 才是私有笔记)。
set -euo pipefail

# 必填(在 shell export,或写进未提交的 .env.local):
#   MODEL_PATH  GLM-4.5-Air 权重目录(绝对路径,见本地 infra 笔记)
#   HF_HOME     指向放权重的 cache 盘
: "${MODEL_PATH:?need MODEL_PATH (GLM-4.5-Air weights dir)}"
: "${HF_HOME:?need HF_HOME (cache dir)}"
export HF_HOME

# 锁空卡(用前 nvidia-smi 看占用)。
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"

TP="${TP:-8}"        # A100 是 PCIe(非 NVLink):大 TP all-reduce 较贵,可试 TP=4 + DP(docs/09)
PORT="${PORT:-8000}"

# MTP(C1):GLM 自带 MTP 头;基线先关,ENABLE_MTP=1 打开(num_speculative_tokens=1 常最优)。
MTP_ARGS=()
if [[ "${ENABLE_MTP:-0}" == "1" ]]; then
  MTP_ARGS=(--speculative-config '{"method":"mtp","num_speculative_tokens":1}')
fi

# 具体 flag 以届时 `vllm serve --help` 为准。
vllm serve "${MODEL_PATH}" \
  --tensor-parallel-size "${TP}" \
  --dtype bfloat16 \
  --port "${PORT}" \
  "${MTP_ARGS[@]}"
