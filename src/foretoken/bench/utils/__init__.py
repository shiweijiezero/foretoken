# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""评测台横切工具(不含回放核心 / 报告逻辑):被 core 与入口层复用的纯助手。

- `timer.py`:流式生成计时 `StreamTimer`(首 token / 结束 → TTFT/TPOT/E2E)。
- `wandb_log.py`:可选 WandB 上报(由 replay 的 `--wandb-project` 触发)。
"""
