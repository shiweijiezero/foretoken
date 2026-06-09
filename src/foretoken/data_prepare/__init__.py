# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""数据准备(data_prepare):缝合真实数据源,打包为可复现的评测数据集。

与评测台(bench/)分工:data_prepare 生成负载,bench 回放负载。单一真实源各缺一半:

- Mooncake trace 有真实并发 / 时序 / 会话结构(hash 前缀链),但无真实文本、无 session id;
- 真实多轮对话(kimi-mtp-dataset)有真实文本,但无并发 / 时序。

缝合方式:由 Mooncake 重建会话与时序结构,填入真实多轮对话内容,得到会话内累积复用(由内容决定、
配置无关,不复刻 Kimi 的 512 块量化)。

- make_workload.py:reconstruct_sessions(去尾满块前缀重建会话,自适应防误连)/ group_by_session
  (自带 session id 的 trace)+ fill_sessions(逐轮填入累积 prompt)。
- build_dataset.py:各 config 各作为一个 split,打包为 parquet + dataset card,供 load_dataset
  直接复用(入口 scripts/build_dataset.sh)。

产出两字段:timestamp_ms / prompt,供 bench/replay.py 回放(输出长度交回放阶段统一 max_tokens 上限,
不预设)。核心缝合逻辑用标准库实现、可单测;读 HF 数据集 / 打包 parquet 走 datasets。

可切换数据源:trace 用 reconstruct_sessions(hash 重建)或 group_by_session(自带 session id);
content 用 to_turns 的字段 / 角色参数 + fill_sessions 的 turns_fn 适配不同格式
(默认 Mooncake + ShareGPT)。

当前仅文本多轮对话(to_turns 跳过多模态条目);多模态内容(VLM 图文 / VLA 动作 / omni 全模态)
暂不支持。
"""
