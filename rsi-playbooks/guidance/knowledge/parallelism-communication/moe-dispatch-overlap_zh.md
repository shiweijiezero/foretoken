<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过微批与块级计算重叠隐藏 MoE 通信

[English](moe-dispatch-overlap.md) | 简体中文

## 现象与动机

每 rank token 很少时，dispatch 与 combine 可能主导 decode。异步接口本身不会消除 dispatch 到专家 GEMM、combine 到下一层的依赖。有效重叠需要独立工作，或更细粒度的生产完成信息。

## 原理与分析

只有与搬运重叠的计算独立于该搬运，才会缩短关键路径。Dispatch 先产生有效专家行，GEMM 才能消费；combine 完成后下一层才能使用结果。整核完成信号比逐块信号暴露的重叠机会少。资源争用可能同时拉长通信与计算，重叠时间不能简单取两者孤立耗时的最大值。

## 设计

通过两个合法 microbatch 创建独立工作，或在单批内暴露细粒度就绪。双批重叠 DBO 将一个 microbatch 的通信与另一个的 attention／专家计算重叠；单批重叠 SBO 在整个 GEMM 结束前发送已完成的专家行块，需要生产者配合。

Stream 与完成句柄保持 dispatch→GEMM→combine 依赖。通信仍使用 SM，并争用 HBM／共享内存；等待中的消费者必须给生产者留下资源。单个普通 decode 请求不提供两个有用 microbatch，除非其他执行模式扩展工作。

## 实现

DeepEP V2.5 主线执行过程如下：

1. 在通信 stream 启动异步 dispatch，执行独立工作，仅在专家需要输入前等待。
2. 取得展开行、路由元数据与 handle。关闭 CPU 同步时使用 GPU 接收计数，不能把分配容量当成有效行数。
3. 计算专家输出，在文档指定计算边界应用路由权重；主线 combine 对传入输出求和。
4. 启动 combine，与其他工作重叠，在下一依赖消费者读取前等待。

vLLM DBO 让全部 DP rank 同意拆分，交替执行两 microbatch 的 dispatch／计算／combine，将适用调度记录进完整 CUDA Graph。第二 microbatch 为空时可关闭拆分。`antgroup-opt` 的 SBO 由 GEMM 发布块完成信号，combine-send 以 acquire 语义等阈值、发送该块，所有块完成后发布专家完成状态。

引用 NVIDIA V2.5 路径要求 Hopper+、CUDA 13.1+、PyTorch 2.10+、NCCL 2.32.3+，节点内 NVLink、跨节点 RDMA。Dispatch 接受 BF16 或带 scale 的 FP8，combine 使用 BF16。V1／NVSHMEM 分支具有不同 API。

## 影响与效果

DBO 以真正独立计算隐藏通信，SBO 提前释放完成块以缩短 combine 尾部，两者仍有同步与信号成本。重叠若因资源争用拖慢 GEMM，更大的重叠区间仍可能让层更慢。

DeepEP V2.5 主线明确使用 GPU SM 执行 EP dispatch／combine，不支持零 SM RDMA EP。SBO PR #483 合入的是 `antgroup-opt` 而非 V2 主线；引用状态下 #453 尚未合并。批适用性与版本接入决定实际执行哪种机制。

## 参考

- [主线 93eb6eb2](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [6849ca69 的 DBO 设计](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/design/dbo.md)
- [PR #483](https://github.com/deepseek-ai/DeepEP/pull/483)
- [#453](https://github.com/deepseek-ai/DeepEP/pull/453)
