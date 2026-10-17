<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 降低请求延迟：拓扑感知的缓存与副本放置

[English](cache-local-placement.md) | 简体中文

## 现象与动机

新增副本可能增加算力，却降低前缀复用或挤满共享缓存链路。暖状态集中有利于亲和性，也会形成流量汇聚热点。放置决定容量与状态在哪里，路由决定哪些请求使用它们。

## 原理与分析

对可行位置比较 `T = queue + restore + uncached_prefill + decode`。GPU 本地命中、主存恢复和远端恢复的成本不同；复用首先要求 checkpoint、位置语义、缓存 dtype 与 connector 布局兼容。

例如 3 GiB 状态通过有效 20 GB/s 路径恢复，payload 至少需 161 ms，还未包含准备和竞争。同一前缀若能在 40 ms 重算，重算更便宜；若需 500 ms，恢复才可能占优。两条路径之后都处理未缓存后缀，所以最长命中不等于最低延迟。

复制分散竞争，却消耗内存和预热带宽；冷副本分散计算，也可能反复重算相同前缀。已有模型权重缓存能加快启动，但不提供暖请求 KV，两种放置收益不同。

## 设计

按真实共享链路域与负载局部性组织放置，包括 GPU–NIC 亲和性、NVLink/NVSwitch、机架链路、主机 NUMA 和存储带宽。多个模型竞争 GPU 时保留 quota 下界。Dense BF16 attention、MLA 和混合状态的 footprint 不同；FP8 权重不代表远端 KV 便宜，改变 TP 布局还可能要求 gather 或复制。

Controller 拥有 worker 放置与生命周期，缓存存储与 connector 拥有状态搬运，请求层拥有路由。索引报告位置，不分配 worker，也不预留传输带宽。

## 实现

1. 枚举满足设备、并行、伙伴阶段与 quota 要求的位置。
2. 查找兼容前缀，区分存储层级和传输路径，按实际 cache layout 计算字节量。
3. 对预计请求组合估计排队、竞争下恢复与剩余计算，计入预热和重复状态成本。
4. 选择兼顾请求延迟与共享域压力的位置，由 controller 创建 worker，就绪后发布路由容量。
5. Worker 退役时由原 owner 释放本地状态，共享条目遵循其存储生命周期。

llm-d cost-aware memory 限制索引内存成本，不分配 worker；Production Stack 集成 LMCache 查询和负载／缓存路由，不自动迁移。Kueue 提供 quota 下界、借用和抢占。Foretoken 区分 GPU 本地 credit 与恢复后缀，`restoreWeight=0.5`，这些分数是偏好而非传输估计；Encoder/Decode 只用负载。

## 影响与效果

拓扑感知放置避免搬运比重算更慢的状态，减少集中的恢复流量，代价是预热、重复权重／状态与共享存储容量。引用 AFD 研究建模 B200、FP8 MoE 在 scale-up／scale-out 链路上的 P/D 伙伴放置，说明链路相关执行成本，不是通用的实测放置加速。

## 参考

- [llm-d cost-aware-memory 实现](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/kvcache/kvblock/cost_aware_memory.go)
- [Production Stack 路由代码](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [llm-d Kueue 指南](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/kueue-rebalancing/README.md)
- [Foretoken 路由 owner](../../../../data-plane/frontend/src/router/README_zh.md)
- [AFD 设计空间研究](https://arxiv.org/html/2605.28302v1)
- [KV 缓存](../kv-cache/)
- [分离式服务](../disaggregated-serving/)
