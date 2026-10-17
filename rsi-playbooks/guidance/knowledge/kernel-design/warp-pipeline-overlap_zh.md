<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按 warp 职责分工，重叠加载、计算与写回

[English](warp-pipeline-overlap.md) | 简体中文

## 现象与动机

内核依次等待输入、计算矩阵和写回结果时，不同硬件单元可能交替空闲。各阶段资源需求不同，让所有 warp 都执行同一顺序，会限制阶段重叠。

## 原理与分析

Warp specialization 让不同 warp 组分别承担加载、计算和输出，通过共享内存槽位与 barrier 交接。独立工作和资源充足时，稳态每块间隔接近最慢阶段 `max(加载, 计算, 写回)`，而非三者串行相加。

启动、排空和同步仍有成本。很小的 decode 矩阵可能还没进入稳态就已结束；更多重叠也意味着更多同时存活的缓冲区，可能降低驻留度。

## 设计

生产者使用输入环形缓冲区，计算组消费就绪数据并释放槽位；epilogue 组转换完成的累加器并发出存储。双缓冲累加器让下一轮矩阵运算与上一轮输出处理重叠。

不同职责分配不同寄存器预算。Epilogue 分片降低暂存内存需求，但增加部分操作。Triton Gluon 的逐元素示例针对 Hopper 及更新设备，`tcgen05` 矩阵示例要求 Blackwell。

## 实现

一次流水循环遵循以下依赖：

```text
加载组：等待空槽 → 声明字节数 → TMA 加载
计算组：等待输入就绪 → 发出 MMA → 等待输入消费完成 → 释放槽位
输出组：等待累加器完成 → 转换 → 暂存 → TMA 存储
存储完成：等待写回结束 → 释放输出槽
```

复用槽位时切换 barrier phase，防止旧完成信号满足新等待。普通共享内存访问与 TMA 操作之间还需要异步代理 fence；barrier 到达本身不能代替这项访存顺序。

实现联合选择职责 warp 数、寄存器预算、输入缓冲深度、累加器双缓冲和输出分片。[持久化调度](gemm-persistent-tiles_zh.md)决定连续领取哪些输出块，warp 分工决定块内怎样推进流水。

## 影响与效果

Gluon 教程在 GB200 的 32K×32K 逐元素加法示例中报告：TMA 基线达到 5.89 TB/s，warp-specialized 版本达到 5.98 TB/s。这是在已有异步基线之上，进一步重叠阶段得到的增量改善。

矩阵负载可以借此隐藏输入或 epilogue 延迟，但额外 warp、缓冲与 barrier 会占用驻留和并行通信所需资源。有效设计应持续供给真正的瓶颈阶段，同时避免让其他阶段变慢。

## 参考

- [Gluon warp specialization 教程](https://triton-lang.org/main/getting-started/tutorials/gluon/warp-specialization.html)：同步、流水设计与 GB200 数据。
- [实现](https://github.com/triton-lang/triton/blob/4ee1a827440d453ca276ded4219190d5e7346b8b/python/tutorials/gluon/08-warp-specialization.py)。
- [CUTLASS GEMM 结构](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md)：Hopper cooperative／ping-pong 与架构相关分工。
