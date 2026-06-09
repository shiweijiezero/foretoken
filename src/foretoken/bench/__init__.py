# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""评测台(bench):闭环回放真实会话负载,采集 TTFT/TPOT、算吞吐与 goodput,出可复现记录。

- `replay.py`:命令行入口,解析参数 → 构造负载 → 起引擎回放 → 写记录(见 `bench.sh`)。
- `core/`:回放核心——`types`(TurnResult)、`workload`(加载 / 窗口 / 采样 / 调度 / goodput)、
  `vllm_engine`(进程内自起 `AsyncLLM`、闭环回放、逐 iteration 监控);进程退出即释放 GPU。
- `report/`:聚合(延迟分位 / 原始吞吐 / goodput SLO 阶梯)+ 人读产物(`summary.md` / 图 / `INDEX`)。
- `utils/`:横切助手——`timer`(流式计时)、`wandb_log`(可选 WandB 上报)。

采样 + 引擎配置加载见顶层 `foretoken.config`(读 `config/models/*.toml`)。

自建闭环回放(而非 `vllm bench serve`)的原因:vLLM 的 `timed_trace` 仅接受 hash、`custom` 不含
timestamp,均无法同时承载真实 prompt 与真实到达时刻;且评测需闭环(下一轮 prompt 用模型现场回复)。
"""

from __future__ import annotations
