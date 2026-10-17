<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按消息形状选择 Collective，减少张量并行同步开销

[English](tp-collective-selection.md) | 简体中文

## 现象与动机

张量并行 TP 通过切分权重容纳或加速模型，却也在层内引入反复同步。小 decode 消息可能受延迟限制，大 prefill 消息可能受带宽限制。增加 TP 会缩小本地矩阵乘法，也可能增加通信占比。

## 原理与分析

列并行投影计算不同输出特征片段；后续行并行投影计算不同输入特征片段产生的部分和。若下一阶段需要完整复制输出，各 rank 通过 all-reduce 相加。若消费者可以保留 token 分片，则 reduce-scatter 只产生本 rank 片段，在之后需要完整行的位置再 all-gather。这改变了阶段间张量职责，不能只替换通信而不修改消费者。

`T` 个 token、hidden 宽度 `H`、元素字节数 `s`，一个完整激活大小为 `B=T*H*s`。`p` 个 rank 的理想 ring all-reduce，每 rank 流量约为 `2*(p-1)/p*B`。这是带宽模型，不是延迟预测；特别是 `T=1` 时，启动、同步、拓扑和协议更重要。

## 设计

引擎在生产者与消费者交接处选择通信，此时输出形状、复制或分片归属已确定。归约 dtype 与 batch-invariant 数值行为约束哪些算法可以互换。

按消息形状与实际 peer／multicast 域选择支持路径。旧 IPC all-reduce 不等于 MNNVL，普通跨节点 InfiniBand 不提供 NVLink multicast。尽可能让频繁 TP 通信留在高速域。Bucket 只合并独立就绪的归约，不能跨越有依赖的非线性计算。

## 实现

引用版本 custom collective 按下列决策执行：

1. 根据生产者／消费者的张量归属确定 all-reduce、all-gather 或 reduce-scatter。
2. 检查该操作的 dtype、字节对齐、连续性、组大小、架构与 peer／multicast 存储可达性。
3. 旧 custom all-reduce 拒绝超过八 rank，虽然外围对象为其他操作支持更大组。Eager 先复制到注册缓冲区，图执行可直接使用已注册图地址。
4. 适用的 MNNVL reduce-scatter 按架构／大小与已初始化 multicast 存储选择 Lamport、multimem 或其他通信器。禁用算法切换的 batch-invariant 路径保持其归约方式。
5. 执行选中 collective，缓冲区保留到完成，再允许依赖层消费结果。

`custom_all_reduce.py` 拥有旧选择器；MNNVL all-gather／reduce-scatter 与低 SM multimem 属于不同源码路径。FP16/BF16、FP32 与量化 GEMM 输出不意味着累加方式相同。

## 影响与效果

按形状选择可在实际消息大小下避免不支持或昂贵路径。已注册图地址不仅减少启动，还可省 eager 暂存拷贝。Reduce-scatter 只有在下游归属同步改变时，才减少复制输出存储。

更高 TP 扩大权重容量、缩小本地 GEMM，但增加协调，可能让单 token decode 受延迟限制。大 bucket 减少启动，也延迟首个消费者、延长缓冲区寿命。PP 将部分 TP 同步换成阶段依赖与气泡，因此最大 TP 不一定延迟最低。

## 参考

- [6849ca69 的 custom collective 实现](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/device_communicators/custom_all_reduce.py)
- [并行部署文档](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/parallelism_scaling.md)
- [流水线微批均衡](pipeline-microbatch-balance_zh.md)
