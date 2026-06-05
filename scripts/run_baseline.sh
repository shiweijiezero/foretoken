#!/usr/bin/env bash
# P0 基线:对运行中的 GLM-4.5-Air 服务运行 benchmark,测量 TTFT/TPOT/goodput。
#
# benchmark 分工(docs/10):AIME / GPQA / LiveCodeBench 偏向 MTP(短输入、长输出);
# SWE-bench(agent)偏向 KV(大型共享前缀、累积历史)。
#
# 用法:
#   [PORT=...] [MODEL=...] [DATASET=...] [REQUEST_RATE=...] bash scripts/run_baseline.sh
#
# 环境变量:
#   PORT          服务端口(默认 8000)
#   MODEL         模型名(默认 GLM-4.5-Air)
#   DATASET       vllm bench serve 的 --dataset-name(默认 custom;Mooncake 缝合见
#                 src/foretoken/data_prepare/stitch.py)
#   REQUEST_RATE  请求速率(默认 inf)
set -euo pipefail

PORT="${PORT:-8000}"
MODEL="${MODEL:-GLM-4.5-Air}"
DATASET="${DATASET:-custom}"
REQUEST_RATE="${REQUEST_RATE:-inf}"

# --goodput 的 ttft / tpot 阈值(ms):默认取 MLPerf v5.1 DeepSeek-R1 档(TTFT<=2s / TPOT<=80ms),
# 按场景调整。
vllm bench serve \
  --backend openai \
  --model "${MODEL}" \
  --base-url "http://localhost:${PORT}" \
  --dataset-name "${DATASET}" \
  --request-rate "${REQUEST_RATE}" \
  --goodput ttft:2000 tpot:80 \
  --save-result
