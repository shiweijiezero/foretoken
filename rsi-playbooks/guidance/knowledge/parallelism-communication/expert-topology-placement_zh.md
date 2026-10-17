<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过拓扑感知的专家副本放置平衡计算与链路负载

[English](expert-topology-placement.md) | 简体中文

## 现象与动机

专家布局能让 token 数均衡，却可能把大量流量送过较慢边界；优先本地性也可能留下热点 rank。合理目标应同时考虑专家执行和 token、输出、迁移权重所经过的链路。

## 原理与分析

复制改变物理目标图，不改变逻辑路由。附近 GPU 的副本可以分摊工作且不增加网络域跨越，但前提是实际 dispatch 分配选择了它。远端副本则可能平衡计算，却增加输入与返回流量。

例如 hidden 宽度 `H=4096`、BF16 激活，有 1000 行专家计算跨节点，单向原始行 payload 为 `1000*4096*2=8,192,000` 字节。这不包含元数据、scale、头部、对齐，也没有考虑去重；多个选中专家同属一个目标时，通信可能复用输入行。输出返回又是另一段流量。输入去重与独立返回流量，使总字节数不同于简单的 top-k 倍数。

## 设计

保持逻辑路由不变，采用分层物理放置：将模型定义的专家组放到节点，在节点内创建冗余专家，再将每副本工作分配到本地 GPU。只有 dispatch 实际选择近端副本，本地性才有收益。冗余权重显存会与 KV 容量竞争。

LPLB 的 `r2o` 通过 cube、hypercube 或 torus 图表达副本关系。Cube 支持八 GPU 子组，torus 可连接本地邻居与邻节点。实际通信域由 GPU/NIC 可达性与共享流量决定，不能只看节点数。

## 实现

引用版本 vLLM 默认 EPLB 策略执行分层算法：

1. 汇总各模型专家组需求，按需求降序，将每组放入负载最轻且未满的节点 pack；各 pack 的条目容量相同。
2. 本地各专家从一份副本开始，每个冗余槽位选择 `demand / replica_count` 最大的专家增加副本，再更新副本数。
3. 将物理副本按单副本负载降序，贪心放入负载最轻且未满的本地 GPU pack，发布逻辑到物理映射。
4. 专家组／节点整除关系不支持分层时退回全局 packing，该路径没有相同本地性保证。

新槽位需要权重时，DeepEP 主线 `lb_prefetch_weights` 使用调用方提供的冗余映射与预留存储，通过 NVLink 搬运权重和量化 scale。域内所有 rank 都须参与，无槽位 rank 也一样。完成后由调用方重路由 token、执行专家计算。副本须保持权重与全部 scale 等价；改变训练路由 bias 或逻辑选择属于模型变化，不是物理放置。

## 影响与效果

分层放置可将专家输入／返回流量留在较快域，冗余副本则分摊热点计算。全局 packing 可能改善计算均衡，却增加远端流量。因此取舍要看最慢 rank 与最忙链路，而不只看 token 数。

冗余消耗权重显存与迁移带宽，频繁变化搬运的权重可能超过省下的 token 流量。DeepEP 主线提供 NVLink 预取原语，不是完整服务副本策略。PR #500 属于 `antgroup-opt`，其能力不能当作主线能力。

## 参考

- [6849ca69 的默认 EPLB 策略](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/policy/default.py)
- [LPLB 0490f794](https://github.com/deepseek-ai/LPLB/tree/0490f79452f7ef277e814449600b1b1dd4c663b3)
- [DeepEP 主线 93eb6eb2](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [#500](https://github.com/deepseek-ai/DeepEP/pull/500)
- [在线副本分配](../moe-execution/batch-replica-assignment_zh.md)
- [窗口专家均衡](../moe-execution/expert-window-rebalancing_zh.md)
