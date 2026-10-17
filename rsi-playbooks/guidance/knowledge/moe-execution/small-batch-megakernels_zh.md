<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按小批 MoE 工作量选择 megakernel 分工

[English](small-batch-megakernels.md) | 简体中文

## 现象与动机

单请求 decode 可能选择多个专家，每专家却只有一行。面向大批量的 grouped GEMM 调度此时主要加载权重、协调执行单元。分开 dispatch、计算与 combine 带来启动边界；megakernel 消除边界后，又必须在通信与计算之间分配共享 GPU 资源。

## 原理与分析

持久 MegaMoE 内核由不同 warp 搬运 token、发起专家矩阵乘法、处理收尾和返回输出，争用 SM、寄存器、共享内存与带宽。更多通信 warp 可以缩短到达等待，也会减少计算资源；`m_e` 很小时，较宽输出 tile 可能延长资源占用。

工作区容量 `C` 不等于有效 token 数 `T`：`C=2048` 的存储可以反复只处理 `T=8`。容量决定存储和部分编译选择，有效路由决定真实工作。仅按满容量调优，可能不适合大多数低负载 step。

## 设计

FlashInfer 提供分离式与融合后端，按架构和 dtype 配置 profile。引用的 SM100 NVFP4 profile 改变输出 tile 宽度和返回结果的角色：收尾 warp、复用 dispatch warp，或独立 warp。这些选择重新分配有限资源，并不是无成本增加并行度。

离线选择配置，预先准备工作区。当前架构文档有按有效 token 的调优记录与预建 profile，调优笔记旧章节仍描述容量选择；实际选择须遵循当前代码路径及其支持的索引。池化 profile 共享存储，并非独立并发通道。

## 实现

1. 初始化专家权重/scale 和支持的容量 profile，按架构、格式、形状及支持的有效 token 桶使用离线调优记录。
2. 各 EP rank 按相同顺序 collective 创建容量工作区，预热后捕获图；相关图退役前保持存储有效，之后由所属层销毁。
3. 每步在容量 profile 内，按 EP 全组最大的 padding 后 token 数选择记录桶：优先精确匹配，其次最小的较大桶，最后最大的较小桶；按格式准备或量化有效激活。
4. 用该配置执行分离的 dispatch/compute/combine，或 MegaMoE；融合路径搬运有效行，完成 FC1/激活/FC2，再按选定 warp 分工返回加权结果。
5. 工作区复用保持 stream 顺序，消费者读取前完成输出复制。

需要所有 rank 同步编译计时的调优不进入请求路径。SM90、SM100 系列等后端各有支持范围；BF16、NVFP4、MXFP8 使用不同数值表示。相关成本见[输入收集融合](moe-gather-fusion_zh.md)与[通信重叠](../parallelism-communication/moe-dispatch-overlap_zh.md)。

## 影响与效果

MegaMoE 能减少启动和中间搬运边界，却不能为单行专家创造 token 间权重复用，低负载仍可能受权重带宽限制。上游调优记录有 combine 量化或核内归约在小批量变慢、在大批量获益的情况；更多融合、更小 payload 并非始终更优。

预先准备输入的微基准不包含每步激活准备；其内核计时没有覆盖量化、输入搬运和输出复制，这些仍属于服务延迟。`C=2048,T=8` 示例用于区分存储与有效工作，不是性能测量。

## 参考

FlashInfer [MoE-EP 架构](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/docs/design_docs/moe_ep_architecture.md)及 [SM100 调优笔记](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/flashinfer/moe_ep/kernel_src/sm100/cutedsl_megamoe/TUNING.md)，commit `a8e747c3`。
