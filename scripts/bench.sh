#!/usr/bin/env bash
# 闭环基线启动器:从仓库根运行 replay CLI,基准参数原样透传。
# 参数与默认值见 `python -m foretoken.bench.replay --help`;引擎进程内自起、脚本退出即释放 GPU。
#
# 用法:
#   bash scripts/bench.sh --model <weights> --config config/models/<m>.toml \
#     --split conversation --window 0:10 --n-requests 200
#
# 可选环境变量:
#   VENV       Python venv 目录(默认 <repo>/.venv;存在则用,否则用 PATH 中的 python3)
#   CUDA_HOME  若设则把其 bin 前置到 PATH(部分环境的 flashinfer JIT 需 nvcc>=12)
# 部署相关(CUDA_VISIBLE_DEVICES / HF_HOME 等)由调用者按自己环境设置,例如:
#   CUDA_VISIBLE_DEVICES=0 HF_HOME=~/.cache/huggingface bash scripts/bench.sh --model ... --config ...
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO}/.venv}"
if [ -x "${VENV}/bin/python" ]; then
  PY="${VENV}/bin/python"
  export PATH="${VENV}/bin:${PATH}"
else
  PY="$(command -v python3 || command -v python)"
fi
[ -n "${CUDA_HOME:-}" ] && export PATH="${CUDA_HOME}/bin:${PATH}"

# -u 不缓冲(summary 实时刷出);基准参数透传给 replay 的 CLI。
exec env PYTHONPATH="${REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${PY}" -u -m foretoken.bench.replay "$@"
