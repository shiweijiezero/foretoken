<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用持久化分块调度复用 GEMM 准备工作

[English](gemm-persistent-tiles.md) | 简体中文

## 现象与动机

矩阵指令已经高效，GEMM 仍可能因线程块重复初始化、权重访问局部性不足或尾部工作不均而变慢。Decode 和小批专家计算的输出块并行度有限，更容易暴露这些成本。

## 原理与分析

普通分块 GEMM 为每个输出块启动一个线程块，共有 `ceil(M/BM) × ceil(N/BN)` 个块。持久化执行只启动有限执行单元，每个连续处理多个块，复用流水准备。

持久化不会自动消除尾浪。100 个等成本块交给 80 个执行单元，只有 20 个需要处理第二块。调度改变的是哪些块由同一单元处理，以及准备、加载、计算和输出如何重叠。分组顺序可以改善缓存复用，但块成本不等时也可能加剧不均衡。

## 设计

执行单元跨输出块保留输入环形缓冲区和矩阵操作状态。TMA 向共享内存槽位加载，消费者进行矩阵计算；输出转换和存储可以与下一个块重叠。

Triton Gluon 提供普通与分组调度。Hopper 的 warp-group MMA 累加器占用寄存器，Blackwell 的 `tcgen05` 路径使用 tensor memory。分块大小、缓冲数量及存活区间共同决定资源占用和驻留度。

## 实现

持久化执行单元的结构为：

```text
初始化输入缓冲区、barrier 和矩阵操作状态
遍历调度器分配给本单元的输出块：
    遍历 K 维归约片段：
        等待空闲输入槽位
        异步加载操作数
        等待该槽位的就绪 phase
        发出矩阵运算
        等待操作数消费完成，再释放输入槽
    转换累加结果，写回输出块
释放共享资源前等待所有写回完成
```

TMA barrier 记录预期字节数，槽位复用时切换 phase。输入与 epilogue 只有在原使用者完成后才能复用存储。M/N 较小时，split-K 可增加并行工作，但额外需要部分结果写回和归约，属于另一种工作划分。

## 影响与效果

持久化调度摊薄线程块准备成本，并可改善权重局部性、重叠输出处理。收益来自这些成本的减少，而非提高设备的矩阵算力上限。

静态分配面对不等成本块可能留下长尾；更深缓冲会增加存活状态并降低驻留度。占满所有 SM 的内核也可能拖慢原本要重叠的通信。小批专家计算还受真实路由产生的分块分布影响，见 [MoE 执行](../moe-execution/)。

## 参考

- [Triton Gluon 持久化教程](https://triton-lang.org/main/getting-started/tutorials/gluon/persistence.html)：调度与缓冲生命周期。
- [教程实现](https://github.com/triton-lang/triton/blob/4ee1a827440d453ca276ded4219190d5e7346b8b/python/tutorials/gluon/07-persistence.py)：Hopper 与 Blackwell 路径。
- [CUTLASS 高效 GEMM](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md)：持久化 cooperative／ping-pong、epilogue 与 split-K。
