<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断延迟瓶颈：从 trace 重建因果关键路径

[English](latency-critical-paths.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

累加算子时长不能还原请求延迟：CPU launch 可先于设备完成返回，stream 会重叠，父 span 包含子 span，collective 可能在等待迟到 rank。按累加值排序，会优先处理长旁路任务，却漏掉阻塞目标完成事件的短启动或同步。

## 原理与分析

区间并集去除重叠重复计数：`|A∪B|=|A|+|B|−|A∩B|`，但不解释因果。依赖 DAG 中，节点 v 的时长 dᵥ、就绪时间 rᵥ，完成时间为 `cᵥ=dᵥ+max(rᵥ, max cᵤ)`，u 为前驱。给定建模的资源顺序，最长必要路径决定完成。

解释性算例中，4 ms 计算与 3 ms 通信重叠，随后执行依赖它们的 1 ms 阶段，总时长为 5 ms 而非 8 ms。通信缩为 2 ms 不改变总时长；计算缩为 2 ms 后为 4 ms。真实重叠可能通过资源竞争拉长两项。

## 设计

先选目标：首引擎 token、首可见文本、末 HTTP 事件或取消后的 KV 释放。保留进程、线程、rank、stream、step 和 correlation 身份；边来自真实 launch 关联、流顺序、事件和消费依赖，不来自时间相邻。

观察到的资源等待与执行分别表示；共享资源实际串行时保留调度顺序。跨 rank 边需要 collective 身份和对齐时钟。启动、编译和 capture 与稳态分窗；图级 trace 可能隐藏内部节点。

## 实现

两个计算分别回答不同问题：

```text
busy_union(intervals)：
    按开始时间排序
    合并 start <= current_end 的区间
    累加合并后区间长度
critical_path(nodes, edges, sink)：
    只保留 sink 的祖先
    按拓扑顺序遍历 v：
        若有前驱，找完成最晚的 u
        无前驱时 start[v] = release[v]
        有前驱时 start[v] = max(release[v], completion[u])
        completion[v] = start[v] + duration[v]
        仅当 u 决定 start 时记录 parent[v] = u
    从 sink 沿 parent 回溯，保留路径上的就绪与等待
```

分配成本前拆开 inclusive span 的自身工作和子节点。纳入实际主机失调度与未满足事件等待，避免把空闲视作零成本。将重建完成与实测目标时间比较，定位遗漏范围。Nsight correlation 与 Perfetto 区间查询支持分析；Foretoken 的 PyTorch、Nsight、mcTracer 覆盖范围不同，缺失的跨进程或设备关联不能靠推测补边。

## 影响与效果

并集区分真实忙碌墙钟时间与重叠／嵌套，DAG 区分暴露依赖与隐藏或旁路工作，rank 到达记录区分会合等待与传输。这避免用算子总量夸大加速空间，并指出哪个阶段必须更早完成。细粒度 tracing 有开销，缺少资源或前端覆盖的图则会低估实际路径。

## 参考

- [Nsight Systems 指南](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)：CPU／CUDA 关联、进程范围、图粒度、开销及跨节点时钟。
- [Perfetto Trace Processor](https://perfetto.dev/docs/analysis/trace-processor)：slice 的时间戳、时长与查询；区间查询本身不识别因果边。
- [SGLang overlap utilities](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py)：引用版本的发布、设备事件、异步复制和消费者 fence。
- [Foretoken 剖析指南](../../../../benchmarks/docs/profile/README_zh.md)：`e6ae3df6` 的捕获准备、产物保存和独立测速。
