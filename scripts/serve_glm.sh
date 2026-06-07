#!/usr/bin/env bash
# 启动 GLM-4.5-Air 的 OpenAI 兼容服务(真实推理基线),A100 8x80GB。
#
# 注意:
#   - A100(sm80)无原生 FP8,GLM-4.5-Air 须用 BF16 权重;FP8 变体在 Ampere 上的 MoE 路径会报错。
#   - 路径经环境变量传入,勿将内网路径写入本脚本(私有笔记见 infra/servers.md)。
#   - serve 默认对齐 config/models/GLM-4.5-Air.toml;采样在回放侧(replay 读同一 config)。
#
# 用法:
#   MODEL_PATH=<weights> HF_HOME=<cache> VENV=<venv dir> \
#     CUDA_VISIBLE_DEVICES=1,3,4,5 PORT=18001 bash scripts/serve_glm.sh
#
# 环境变量:
#   MODEL_PATH            权重目录(必填)
#   HF_HOME               模型缓存目录(必填)
#   VENV                  uv venv 目录;置则前置其 bin 到 PATH(worker 子进程需找到 vllm/ninja)
#   CUDA_HOME             nvcc>=12 的 CUDA(默认 /usr/local/cuda-12.3;flashinfer 运行时 JIT 需 nvcc>=12)
#   CUDA_VISIBLE_DEVICES  锁空卡(默认 0-7;共享机务必先 nvidia-smi 看占用)
#   TP                    tensor parallel size(默认 4)
#   MAX_MODEL_LEN         KV 窗口(默认 131072 = 官方 context;须 >= 采样 max_tokens,否则 vLLM 400)
#   GPU_MEM_UTIL          显存利用率(默认 0.92)
#   PORT                  服务端口(默认 18001)
#   ENABLE_MTP            置 1 启用内嵌 MTP(num_speculative_tokens=1)
set -euo pipefail

: "${MODEL_PATH:?MODEL_PATH 必填:GLM-4.5-Air 权重目录}"
: "${HF_HOME:?HF_HOME 必填:模型缓存目录}"
export HF_HOME

export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.3}"
export PATH="${CUDA_HOME}/bin:${PATH}"
if [[ -n "${VENV:-}" ]]; then export PATH="${VENV}/bin:${PATH}"; fi

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
TP="${TP:-4}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.92}"
PORT="${PORT:-18001}"

MTP_ARGS=()
if [[ "${ENABLE_MTP:-0}" == "1" ]]; then
  MTP_ARGS=(--speculative-config '{"method":"mtp","num_speculative_tokens":1}')
fi

vllm serve "${MODEL_PATH}" \
  --served-model-name GLM-4.5-Air \
  --tensor-parallel-size "${TP}" \
  --dtype bfloat16 \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL}" \
  --port "${PORT}" \
  "${MTP_ARGS[@]}"
