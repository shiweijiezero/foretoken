#!/usr/bin/env bash
# 闭环基线:进程内自起 vLLM 引擎(AsyncLLM)→ replay 回放 foretoken-trace → 进程退出即释放 GPU。
#
# 引擎随本进程生命周期:无独立 HTTP server、无孤儿占卡;关停由 replay 的 engine.shutdown() +
# 进程退出保证(正常 / 报错 / Ctrl-C 都释放)。serve 配方见 scripts/serve_glm.sh(仅供手动起服务调试)。
#
# 环境坑(进程内引擎同样跑 flashinfer JIT,详见 infra/servers.md「起 vLLM」节):
#   - CUDA_HOME 须指向 nvcc>=12 的 CUDA(系统 /usr/bin/nvcc 可能是 11.5)。
#   - PATH 须含 venv/bin(ninja)+ CUDA bin(nvcc)。
#   - 共享机用 CUDA_VISIBLE_DEVICES 锁空卡;起前 nvidia-smi 看占用。
#
# 用法:
#   MODEL_PATH=<weights> HF_HOME=<cache> VENV=<venv> CUDA_VISIBLE_DEVICES=1,3,4,5 \
#     [NAME=GLM-4.5-Air] [SPLIT=conversation] [WINDOW=0:5] bash scripts/run_baseline.sh
#
# 环境变量:
#   MODEL_PATH            权重目录或 HF id(必填;起引擎 + 分词器)
#   HF_HOME               模型缓存目录(必填)
#   VENV                  uv venv 目录(默认 <repo>/.venv)
#   CUDA_HOME             nvcc>=12 的 CUDA(默认 /usr/local/cuda-12.3)
#   CUDA_VISIBLE_DEVICES  锁空卡(默认 0-7)
#   NAME                  config/报告用逻辑名(默认 GLM-4.5-Air)
#   SPLIT                 数据集 split(默认 conversation)
#   WINDOW                时间窗分钟 N 或 A:B(默认 0:5)
#   TAIL_FACTOR           回放墙钟时限系数(默认 2.0,见 replay --tail-factor)
#   SEC_MULTIPLIER        时间缩放(默认 1.0)
#   REPLAY_ARGS           追加给 replay 的原样参数(如 "--engine-param kv_cache_dtype=fp8")
set -euo pipefail

: "${MODEL_PATH:?MODEL_PATH 必填:权重目录或 HF id}"
: "${HF_HOME:?HF_HOME 必填:模型缓存目录}"
export HF_HOME

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO}/.venv}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.3}"
export PATH="${VENV}/bin:${CUDA_HOME}/bin:${PATH}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"

NAME="${NAME:-GLM-4.5-Air}"
SPLIT="${SPLIT:-conversation}"
WINDOW="${WINDOW:-0:5}"
TAIL_FACTOR="${TAIL_FACTOR:-2.0}"
SEC_MULTIPLIER="${SEC_MULTIPLIER:-1.0}"

mkdir -p "${REPO}/tmp"

# -u 不缓冲,summary 实时刷出;引擎在进程内,脚本退出即释放 GPU。
PYTHONPATH="${REPO}/src" "${VENV}/bin/python" -u -m foretoken.bench.replay \
  --model "${MODEL_PATH}" --name "${NAME}" \
  --split "${SPLIT}" --window "${WINDOW}" \
  --tail-factor "${TAIL_FACTOR}" --sec-multiplier "${SEC_MULTIPLIER}" \
  ${REPLAY_ARGS:-}
