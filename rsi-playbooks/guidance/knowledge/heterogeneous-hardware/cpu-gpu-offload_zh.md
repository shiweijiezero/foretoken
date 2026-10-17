<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过 CPU/GPU 放置与卸载平衡容量和延迟

[English](cpu-gpu-offload.md) | 简体中文

## 现象与动机

增加内存可以让模型运行，却不保证更快。CPU 常驻层、连续 GPU 层分区、tensor shard 和每次前向搬权重支付不同成本。按等量内存放置可能让快设备等慢设备，权重卸载则可能每生成一个 token 都增加链路流量。

## 原理与分析

设备 j 应满足 `weights+state+workspace+graph_memory≤usable_memory`。顺序层延迟近似为分区计算加激活传输；TP 层由最慢 shard 加通信决定。按内存比例放置保证容量，不保证计算均衡。

每 token 搬运 S 字节、持续带宽 B 时，仅传输下界就是 S/B。解释性量纲算例：4 GiB 按 32 GiB/s 搬运需 125 ms，尚无计算／同步。CPU 常驻执行改为读取 CPU 权重，层边界则传激活而非全部权重，三者不是同一种卸载。

## 设计

容量优先时使用连续层放置以减少边界，层的 KV／状态由执行 owner 持有。受支持 tensor split 提供层内并行，但反复归约且要求相容架构、内核和分片对齐。

llama.cpp 提供 GPU 层数与 split 比例。引用指南中 experimental tensor 模式要求支持的架构与 flash attention，不接受量化 cache，且无自动 fitting。其性能建议主要面向 CUDA，HIP／RCCL 默认选择不同。vLLM CPU 执行按架构选择 dtype／ISA，并保持线程和内存 NUMA 本地。

## 实现

顺序放置的执行步骤：

1. 按目标上下文计算每设备权重、KV／状态、workspace 和 graph buffer，在内存预算内分配连续且受支持的层。
2. 将各层权重一次加载或转换为 owner backend 表示。CPU 常驻内存在 rank 的 NUMA 节点初始化，并为服务保留主机核心。
3. 执行分区，将激活传给下一分区，等待 copy 完成事件再继续，只返回所需最终结果。

流式权重卸载则分配设备 staging buffer 与 pinned host 存储；当前层计算时，在 transfer stream 提交下一层权重 copy。计算流等 weight-ready，传输流等 last-use 后才能覆盖槽位。链路带宽、staging 容量和真实独立工作限制重叠。

Tensor split 将受支持算子的权重／状态分片，各 rank 执行局部计算并逐层完成必需 collective，不能当作顺序算法互换。混合设备需要相容 backend 路径，CPU 矩阵 ISA 本身不提供 GPU 卸载。

## 影响与效果

层放置和 CPU 常驻扩展可运行的模型／上下文容量。连续分区减少激活边界，提前搬权重可隐藏部分链路工作，但 CPU 带宽、矩阵执行、传输、同步和慢 shard 等待仍有成本。量化只在目标表示和内核兼容时减少字节。能跑更大模型但延迟更高，是容量收益，不是速度提升。

## 参考

- [llama.cpp 多 GPU 执行，commit 77c4540](https://github.com/ggml-org/llama.cpp/blob/77c4540ce4667f4577753fe034a3a8042f510b8f/docs/multi-gpu.md)。
- [vLLM CPU 指令、dtype、NUMA 和线程指南](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/)。
- [llama.cpp token generation 性能入口](https://github.com/ggml-org/llama.cpp/blob/77c4540ce4667f4577753fe034a3a8042f510b8f/docs/development/token_generation_performance_tips.md)；其中示例数字是历史结果，不是当前硬件对比。

 · [并行与通信](../parallelism-communication/) · [知识索引](../README_zh.md)
