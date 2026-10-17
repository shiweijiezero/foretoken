<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 逐批分配专家副本，均衡当前计算负载

[English](batch-replica-assignment.md) | 简体中文

## 现象与动机

按历史负载均衡的专家布局，当前批量仍可能让一张 GPU 等另一张。将某个专家的 token 在副本间均分，也没有考虑目标 GPU 的其他工作。LPLB 处理更快的时间尺度：在已有副本间重分配当前专家求值，不逐批加载权重，也不改变逻辑路由。

## 原理与分析

可用 token 数的 minimax 模型理解目标：

```text
minimize z
subject to sum_r x[e,r] = n_e
           sum_e x[e,r] <= z
           x[e,r] >= 0, only where replica(e,r) exists
```

`n_e` 是当前逻辑专家需求，`x[e,r]` 是分给目标 rank `r` 的需求，`z` 限制各 rank 总量。这是目标解释，不是源码求解器的逐项复述；分配比例还须落实为整数 token-expert 行。

A、B 分别有 80、20 行，其中 A 的 40 行属于 B 也持有的专家，移动 30 行即可使总量各为 50。无副本的工作不能移动。Top-k 下计数的是专家求值而不是请求；只有每行成本接近时，等行数才近似等耗时。

## 设计

逐批规划前固定冗余拓扑。引用的 `Planner` 要求各 rank 冗余槽位数相同，并采用特定映射结构；被复制的逻辑专家恰有两个物理副本。历史 EPLB 先重排原始专家，再为这些槽位挑选热点候选。

批规划交换需求，在副本图上求分配比例，再将逻辑 ID 映射到物理槽位。槽位须持有等价的专家参数及 scale。逻辑专家和路由权重保持不变，只改变执行目的地。

## 实现

1. 用 `count_workload` 统计本批逻辑专家 ID。
2. `solve_probs` 同步需求并调用编译求解器；未初始化其 DeepEP 通信路径时，使用配置的进程组。
3. `weighted_select_target` 根据求得比例和逐 SM 计数元数据，为有限行数生成物理目的地。
4. Dispatch 到对应槽位，执行原专家函数，按原始权重和 token 顺序合并。

固定版本 `lplb/planner.py`、`lplb/eplb.py` 管理拓扑和分配。它们使用旧 DeepEP `Buffer` 与 NVSHMEM 同步，当前 DeepEP V2.5 则使用 `EPBuffer` 并移除 NVSHMEM；集成需要适配这一通信边界，不能直接替换依赖。[窗口均衡](expert-window-rebalancing_zh.md)负责较慢的布局变化。

## 影响与效果

LPLB README 报告节点内求解约 100 微秒，跨节点更高。这是研究实现的求解开销估计，不是 decode 加速比，也没有构成模型/硬件基准；极小批量中，它可能超过消除 rank 不均衡所省的时间。

逐批分配无需迁移权重就能响应波动，但受双副本图限制，并增加统计、交换、求解和映射成本。更多副本需要更多显存。Token 数目标没有表达 grouped GEMM 的非线性 tile 效率和拓扑相关 dispatch 成本，因此最大行数下降不必然缩短整层执行。

## 参考

[LPLB commit 0490f794](https://github.com/deepseek-ai/LPLB/tree/0490f79452f7ef277e814449600b1b1dd4c663b3) 的 README、`lplb/planner.py`、`lplb/eplb.py`及[当前 DeepEP](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)。
