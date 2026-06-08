# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""回放核心(不含命令行入口):逐轮结果类型、负载构造纯函数、vLLM 引擎执行层。

- `types.py`:逐轮结果 `TurnResult`。
- `workload.py`:数据加载、时间窗截取、会话级下采样、时间调度、goodput 标尺。
- `vllm_engine.py`:进程内自起 vLLM 引擎(`AsyncLLM`)、闭环回放循环、逐 iteration 监控。

入口在上一级 `foretoken.bench.replay`(argparse + 编排)。
"""
