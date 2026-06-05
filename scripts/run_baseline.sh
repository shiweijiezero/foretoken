#!/usr/bin/env bash
# P0 基线:对已起的 GLM-4.5-Air 服务跑真实 benchmark,量 TTFT/TPOT/goodput。
#
# benchmark 分工(docs/10):AIME / GPQA / LiveCodeBench 压 MTP(短输入 + 超长 output);
#                          SWE-bench(agent)压 KV(巨型共享前缀 + 累积历史)。
set -euo pipefail

PORT="${PORT:-8000}"
MODEL="${MODEL:-GLM-4.5-Air}"
DATASET="${DATASET:-custom}"          # custom / hf;Mooncake 缝合见 src/foretoken/data_prepare/stitch.py
REQUEST_RATE="${REQUEST_RATE:-inf}"

# vllm bench serve 原生支持 --goodput(docs/10):ttft / tpot 阈值(ms);
# 默认值取 MLPerf v5.1 DeepSeek-R1 档(TTFT<=2s / TPOT<=80ms),按场景调。
vllm bench serve \
  --backend openai \
  --model "${MODEL}" \
  --base-url "http://localhost:${PORT}" \
  --dataset-name "${DATASET}" \
  --request-rate "${REQUEST_RATE}" \
  --goodput ttft:2000 tpot:80 \
  --save-result
