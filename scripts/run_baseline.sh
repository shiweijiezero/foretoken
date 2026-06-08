#!/usr/bin/env bash
# 闭环基线启动器:设好进程内引擎所需环境,再把所有参数透传给 replay 的 CLI。
#
# 参数与默认值都在 replay(`python -m foretoken.bench.replay --help`)——本脚本不重复定义,只负责环境。
# 进程内引擎随脚本退出释放 GPU(engine.shutdown() + 进程退出)。
#
# 用法(基准参数直接给 replay,环境用 env 前缀):
#   CUDA_VISIBLE_DEVICES=1 HF_HOME=<cache> bash scripts/run_baseline.sh \
#     --model <weights> --config config/models/<m>.toml --split conversation --window 0:10 --n-requests 200
#
# 环境变量(进程内引擎跑 flashinfer JIT;各机 CUDA / 占卡细节见私有 infra/servers.md):
#   VENV                 uv venv 目录(默认 <repo>/.venv)
#   CUDA_HOME            nvcc>=12 的 CUDA(默认 /usr/local/cuda-12.3)
#   CUDA_VISIBLE_DEVICES 锁空卡  ·  HF_HOME 模型缓存目录
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO}/.venv}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.3}"
export PATH="${VENV}/bin:${CUDA_HOME}/bin:${PATH}"

# -u 不缓冲(summary 实时刷出);所有基准参数透传给 replay。
exec env PYTHONPATH="${REPO}/src" "${VENV}/bin/python" -u -m foretoken.bench.replay "$@"
