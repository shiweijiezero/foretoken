<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按负载窗口复制与迁移专家，分摊持续热点

[English](expert-window-rebalancing.md) | 简体中文

## 现象与动机

专家并行层要等最慢 rank 完成。每张 GPU 保存相同数量的专家，执行负载仍可能不同：语言、任务、层和请求组合会产生持续热点。窗口式 EPLB 通过复制与迁移权重分摊工作，同时保持模型的逻辑专家选择。

## 原理与分析

默认复制策略对估计负载 `w_e`、副本数 `r_e` 使用每副本负载 `w_e/r_e`。例如 `[100,20,20,20]` 且只有一个冗余槽位，复制第一专家后每副本估计降为 50。这是规划值，不意味着 GEMM 延迟减半。

合适的时间尺度应长于单批噪声、短于持续业务变化。短窗口响应快，也容易追逐噪声；长窗口则滞后于新的请求组合。重平衡间隔按引擎 step 计数，墙钟时间随负载变化。即使 token 数相同，tile 和通信不同也可能导致耗时不等。

## 设计

收集历史窗口的全局负载，在固定冗余预算下选择物理到逻辑映射。模型分组允许分层时，默认策略先将专家组分配到节点，再为每副本负载高的专家加副本，最后装入各 GPU；尽量保留已有本地槽位，避免无用复制。

异步 worker 计算布局，逐层暂存所需权重。前向执行消费待处理结果，在复用暂存空间前安装权重及对应映射。量化 scale 是专家表示的一部分，须随权重迁移；图执行在变化期间仍须使用有效存储和映射。

## 实现

1. 从前向执行累计专家统计，间隔到达后将全局估计和旧映射交给默认策略。
2. 按每副本估计负载分配冗余副本，跨 rank 装入槽位，保留不变的本地槽位。
3. 生成权重传输。可选迁移调度合并同一有向 rank 对，并将端点不冲突的传输分批，使每批一个 rank 最多与一个 peer 通信；本地移动无需远程传输。
4. 暂存一层权重和 scale，等待写入完成，再发布待消费结果。
5. 前向侧安装对应专家表示和映射，确认消费后释放暂存区供下一次使用。

固定版本 vLLM 默认策略、async worker 和 `migration_scheduler.py` 管理这些阶段。[逐批副本分配](batch-replica-assignment_zh.md)则处理既有布局内的波动。

## 影响与效果

复制分散持续热点，不改变逻辑 router；保留槽位减少迁移字节，端点不冲突的调度避免同批传输争用。`[100,20,20,20]` 示例解释分配，不是实测加速。

冗余权重和 scale 占用本可用于 KV 的显存。异步迁移仍消耗网络和设备带宽，host 不阻塞也可能使推理延迟上升。快速轮换的热点可能在窗口响应前已经消失。净收益取决于持续偏斜、观测滞后、传输干扰及缓存容量损失，而不只是一张更均衡的统计图。

## 参考

vLLM [commit 6849ca69](https://github.com/vllm-project/vllm/tree/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9) 的[默认策略](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/policy/default.py)、[异步 worker](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/async_worker.py)、`migration_scheduler.py` 及[官方 EP 部署指南](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/)。
