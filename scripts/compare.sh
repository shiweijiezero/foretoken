#!/usr/bin/env bash
# 多组实验对比:全指标对比表 + 对比图(CDF 叠加 / 分位柱 / goodput 柱 / 吞吐柱)。
# 纯后处理(读 run.json / turns.jsonl,不起引擎、不需 GPU/vLLM);每次对比留存到带时间戳的目录、不覆盖。
#
# 用法:
#   bash scripts/compare.sh results/runs/<A> results/runs/<B> ...   # 比指定几组
#   bash scripts/compare.sh results/runs                            # 比全部
# 产出:results/compare/<时间戳>/summary.md(+ en/ zh/ 对比图)。COMPARE_OUT 可覆盖输出目录。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO}/.venv}"
if [ -x "${VENV}/bin/python" ]; then
  PY="${VENV}/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

OUT="${COMPARE_OUT:-results/compare/$(date +%Y-%m-%d_%H%M%S)}"
exec env PYTHONPATH="${REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${PY}" \
  -m foretoken.bench.report --compare "$@" --out "${OUT}"
