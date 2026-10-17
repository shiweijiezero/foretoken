<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 避免流水线积压：联合分配各阶段容量

[English](pipeline-capacity-balance.md) | 简体中文

## 现象与动机

Encoder 或 P 产生状态比传输和 D 消费更快时，增加 worker 会扩大积压。多阶段服务要平衡计算、准入、传输和中间状态驻留。孤立阶段变快，只有完整路径能在延迟及内存预算内吸收输出时才有效。

## 原理与分析

路径 `E→P→X→D` 中 X 为 KV 传输，初步稳定条件是 `λ<min(n_E μ_E,n_P μ_P,μ_X,n_D μ_D)`。容量使用同一负载分布和可比较的请求单位，encoder 项/s 不能直接与文本 token/s 比；D 还包括请求寿命与上下文增长。

到达 8 请求/s，E/P/X/D 容量为 12/10/7/9 时，P 翻倍仍受传输 7 限制；到达保持 8，X 持续积压。稳定阶段 Little 定律给平均驻留请求 `λW`，驻留字节还需按大小加权。少量长 prompt 就可能主导内存。

## 设计

联合分配阶段副本、链路和 HBM，以端到端尾延迟预算为目标。阶段完成是就绪屏障，不是后续所有资源的原子预留。提前预留 D 保护交接，却可能在 E/P 时闲置槽位；延后选 D 避免预留，却暴露下游竞争。

SARA 用 M/G/k 建模 P、M/G/1 建模传输，以 birth–death 近似连续批 D，搜索满足分位约束的算力、带宽和 HBM。Poisson 到达和长度独立是模型假设，不保证适合相关多模态突发。Dynamo 已提供 NIXL embedding 的 E/PD、E/P/D，不同 backend 范围不同。

## 实现

联合容量与执行过程为：

```text
从同一负载取得阶段耗时和状态字节分布
枚举E/P/D数量、传输容量及内存分配
估计阶段排队和端到端分位延迟
在总预算内选择可行分配
按完整路径可持续速率准入工作
执行E，发布完成的embedding descriptor
执行P，保留状态直到KV交接结束
选择兼容D，必要状态就绪后开始
完成或取消后回收中间资源
```

这描述联合分配，不声称已实现原子 reservation。Foretoken Qwen2.5-VL-3B 示例使用共享 encoder 存储、Mooncake P→D 和独立 Pool；workflow 在 P 后选 D，保留 E/P 屏障及清理，扩缩容分别估容量但提交完整服务 cohort。

## 影响与效果

阶段平衡避免产生无法及时消费的 embedding 或 KV。12/10/7/9 示例中，扩 P 不改变稳定速率或 X 过载，改善传输或限制实际准入才作用于瓶颈。中间状态寿命可能长于计算，先成为物理容量限制。

SARA 结合建模／模拟和 A100/SGLang 测量。真实 LLaMA 3.1 8B trace 回放中，峰值阶段 Splitwise、Colocated、经验分配相对 SARA 的最大 goodput 下降分别为 21.5%、45.6%、57.2%。这些是以 SARA 为参照的基线下降比例，不是 SARA 相对各基线的提升百分比。文中的 LLaMA 3.1 8B、Qwen2.5 32B、GPT-3 175B 配置不能全写成统一 dtype 的物理运行。分离增加权重、buffer、启动与故障域；平均容量无法约束大图／长 prompt 相关突发的尾延迟。更多预留能使交接可预测，同时降低利用率。

## 参考

- [encoder 分离文档](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/docs/fern/pages/use-cases/multimodal-serving/encoder-disaggregation.md)
- [SARA 论文](https://arxiv.org/html/2609.26763v1)
- [维护中 E/P/D 示例](../../../../examples/encoder-prefill-decode/README_zh.md)
- [执行工作流](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [扩缩容 owner](../../../../control-plane/internal/autoscaling/README_zh.md)
- [多模态生成](../multimodal-generation/)
