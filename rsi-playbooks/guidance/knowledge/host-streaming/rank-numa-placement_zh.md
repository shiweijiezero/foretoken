<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少 rank 停顿：NUMA 感知主机放置

[English](rank-numa-placement.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

多 rank 副本增加 scheduler、设备 worker、通信推进、分词和输出任务。并行度上升可能减少每 rank 的设备工作，却增加可运行主机任务。整机 CPU 利用率低，也可能让提交下一次 collective 的核心或 cgroup 配额不足。vLLM 的物理核心指导分别计算 API、engine core 和 GPU worker 的需求。

## 原理与分析

配额、亲和性与内存局部性约束不同资源。cgroup 每 100 ms 获得 200 ms 运行预算时，总配额约为两核，即使允许在八个 CPU 上执行。Affinity 不会预留核心或提高配额。NUMA 通常在分配或首次访问时放置页面，先分配 buffer 再绑定 worker 不会搬迁已有内存。

设各 rank 到达 collective 的时间为 aᵣ，完成不会早于 max(aᵣ) 加剩余传输工作。解释性算例中，三个 rank 在 1 ms 到达，一个在 5 ms 到达，传输再花 1 ms；早到 rank 的通信区间约 5 ms，最后一个为 1 ms。主导的 4 ms 偏斜不是链路传输。

## 设计

让每个设备 worker 的 CPU 执行位置与频繁访问的主机 buffer 靠近其 GPU。为 API、scheduler、输出和通信任务保留空间，不把全部物理核心交给 worker 线程。确定亲和性与允许内存节点后再分配 pinned buffer；共享带宽型 buffer 则可能更适合交错放置。

逐 rank 保留配额限流、可运行等待、页面位置与 collective 到达信息。分布式 trace 按步骤和 collective 身份及同步时钟对齐，不直接相减独立节点时间。

## 实现

在 Linux NUMA 主机上执行以下步骤：

1. 根据 GPU PCI 局部性映射 NUMA 节点和物理核心，再与容器 effective CPU／memory set 取交集。
2. 读取包含父级限制的 cgroup 配额层级，合计 API、engine、worker 与库线程池的同时物理核心需求。
3. 创建和首次访问元数据、pinned-copy buffer 前设置 worker affinity 与内存策略，在消费者所在节点初始化页面。更改放置时重建或显式迁移原有远端分配。
4. 按 communicator、操作与步骤分组 collective 记录，计算 `max(aᵣ)−min(aᵣ)`，将迟到 rank 与之前的 CPU runnable wait、throttling、主机 copy 和设备计算关联。
5. 据此区分配额耗尽和远端内存访问，不能只看 CPU 总百分比。

SGLang overlap 路径只为需要它的消费者更新 CPU 长度副本。Foretoken CPU 曲线来自 counter 估算，不是逐 rank 调度 trace。

## 影响与效果

本地分配减少 worker 私有 buffer 的跨 socket 流量；物理核心放置减少运行队列和超线程争用；充足配额消除周期性预算耗尽。这些变化可以降低 collective 到达偏斜，而不改变网络带宽。独占核心会降低副本装箱容量，严格内存绑定也可能限制共享带宽。超线程不等于独立物理核心。

## 参考

- [vLLM 优化文档](https://docs.vllm.ai/en/latest/configuration/optimization/)：物理核心预算与 busy-loop scheduler 竞争。
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)与[NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html)：配额、effective CPU set、pressure、分配与迁移语义，属于动态内核文档。
- [SGLang overlap 工具](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py)：设备和 CPU 元数据边界。
- [Nsight Systems 文档](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)：进程范围、CPU/设备关联、时间归一化与跨节点时钟对齐。
- [Foretoken CPU 观测](../../../../benchmarks/docs/metrics_zh.md#cpu-与内存观测)：`e6ae3df6` 的实现，属于 counter 估算与 trailing window 曲线，不是逐 rank 调度 trace。
