#!/usr/bin/env bash
# 计价成本报告:按 GPU 时价折算各配置每百万 token 成本(原始 / 达成 SLO),横向对比。
# 纯后处理(读 run.json,不起引擎、不需 GPU/vLLM);留存到带时间戳目录、不覆盖。
#
# 用法:
#   bash scripts/costreport.sh results/runs/<A> results/runs/<B> ...   # 比指定几组
#   bash scripts/costreport.sh results/runs                            # 比全部
#   GPU 时价覆盖 / 选 SLO 档:
#   bash scripts/costreport.sh results/runs --gpu-rate A100=1.8 --gpu-rate 4090=0.4 --slo-tier 0
# 产出:results/compare/<时间戳>-cost/cost.md(+ en/ zh/ 柱图)。COST_OUT 可覆盖输出目录。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO}/.venv}"
if [ -x "${VENV}/bin/python" ]; then
  PY="${VENV}/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

OUT="${COST_OUT:-results/compare/$(date +%Y-%m-%d_%H%M%S)-cost}"
exec env PYTHONPATH="${REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${PY}" \
  -m foretoken.bench.report --cost "$@" --out "${OUT}"
