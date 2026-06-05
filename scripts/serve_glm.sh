#!/usr/bin/env bash
# 启动 GLM-4.5-Air 的 OpenAI 兼容服务(P0 真实推理基线),运行于 A100 8x80GB。
#
# 注意:
#   - A100(sm80)无原生 FP8(docs/09),GLM-4.5-Air 须使用 BF16 权重;FP8 变体在 Ampere 上的
#     MoE 路径会报错。
#   - 路径一律通过环境变量传入,勿将内网路径写入本脚本(私有笔记见 infra/servers.md)。
#
# 用法:
#   MODEL_PATH=<weights dir> HF_HOME=<cache dir> [TP=8] [PORT=8000] [ENABLE_MTP=1] \
#     bash scripts/serve_glm.sh
#
# 环境变量:
#   MODEL_PATH   GLM-4.5-Air 权重目录(必填)
#   HF_HOME      模型缓存目录(必填)
#   TP           tensor parallel size(默认 8)
#   PORT         服务端口(默认 8000)
#   ENABLE_MTP   置 1 启用内嵌 MTP(num_speculative_tokens=1)
set -euo pipefail

: "${MODEL_PATH:?MODEL_PATH 必填:GLM-4.5-Air 权重目录}"
: "${HF_HOME:?HF_HOME 必填:模型缓存目录}"
export HF_HOME

# 选择 GPU;运行前用 nvidia-smi 确认占用。
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"

# A100 为 PCIe 互联(非 NVLink),大 TP 的 all-reduce 开销较高,可尝试 TP=4 + DP(docs/09)。
TP="${TP:-8}"
PORT="${PORT:-8000}"

# C1 内嵌 MTP:基线默认关闭,置 ENABLE_MTP=1 启用(num_speculative_tokens=1 通常最优)。
MTP_ARGS=()
if [[ "${ENABLE_MTP:-0}" == "1" ]]; then
  MTP_ARGS=(--speculative-config '{"method":"mtp","num_speculative_tokens":1}')
fi

# 具体参数以 `vllm serve --help` 为准。
vllm serve "${MODEL_PATH}" \
  --tensor-parallel-size "${TP}" \
  --dtype bfloat16 \
  --port "${PORT}" \
  "${MTP_ARGS[@]}"
