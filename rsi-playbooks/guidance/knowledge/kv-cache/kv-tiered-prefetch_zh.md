<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过分层卸载与异步预取隐藏 KV 加载延迟

[English](kv-tiered-prefetch.md) | 简体中文

## 现象与动机

只有读取缓存暴露在关键路径上的时间小于被省掉的计算，命中才有价值。主机内存、SSD 和远端存储扩大了容量，却可能因为暂存、排队和等待完成，让高命中率服务的首 token 延迟反而更差。

## 原理与分析

普通稠密注意力每个 rank 的 KV 字节量可近似为 `D = 2 L N Hkv d b`：`L` 是层数，`N` 是 token 数，`Hkv` 是本 rank 实际保存的 KV 头数，`d` 是头维度，`b` 是每元素字节数。复制 KV 头、MLA 和混合模型应按真实缓存形状计算。例如，32 层、8 个 KV 头、头维度 128、BF16、8,192 token 的未分片缓存约为 1 GiB。假设有效带宽为 20 GiB/s，单纯传输就需要 50 ms，尚未包括查找、缺页、打包和接纳。

可以先用下式拆解：

`T_hit = T_lookup + T_queue + T_stage + max(0, T_copy - T_overlap) + T_remaining`。

替代方案的成本是相同 token 的完整 prefill。并发加载与解码决定有效带宽，链路标称值不包含其争用。`T_overlap` 只计同时进行的有用计算，而非仅仅异步提交。

逐层流水先加载第 0 层，在该层执行剩余计算时加载第 1 层，下一层尚未就绪才等待。稳定阶段接近 `max(T_layer_copy, T_layer_compute)`，仍有启动与排空成本。先完整加载前缀再执行，重叠条件不同；SSD 经主机内存暂存又增加一层流水，主机内存带宽饱和还会让名义上独立的搬运串行化。

## 设计

将放置策略与搬运执行分开。Controller 查找可复用对象、保护源存储、预留目标容量并提交加载；引擎只消费已就绪数据，完成通知再释放搬运持有的资源。这样取消与淘汰才能和异步 I/O 共存。

逐层加载可以让下一层搬运与当前层计算重叠；整前缀加载的依赖不同。预取距离在等待与过早占用目标容量之间取舍，低层还可能需要 SSD 到主机暂存，再做远端或 H2D 搬运。

## 实现

LMCache 的 `prefetch_controller.py` 执行以下生命周期：

1. 为已有 L1 命中加读取锁，对未命中对象提交 L2 查找加锁任务。
2. 解析返回的位置，由策略规划加载；引用版本将放置解析到一个 L1 manager。
3. 预留 L1 目标，再将源读取提交到这些目标。
4. 接纳已完成对象到 L1，发布可用状态，释放对应源与目标锁。
5. 请求取消时，须等在途搬运不再持有目标，才回收未使用的预留。

Dynamo 的 `offload/pipeline.rs` 在 HBM、固定页 DRAM、SSD 与对象存储之间区分策略、组批、前置条件和搬运。逐层引擎还需在 attention 前等待该层就绪；Mooncake 异步提交本身不产生这一调度。

## 影响与效果

分层让可复用缓存超出 HBM 容量，并可用较便宜的加载替代昂贵 prefill。只有真正与有用计算重叠的搬运，才能减少暴露等待。代价包括保存与卸载流量、固定页主机内存、目标预留和淘汰压力。

重复长上下文且传输资源充足时最有利；短输入、低复用或共享 PCIe／主机内存争用，会让低层命中比重算更慢。因此高命中率本身不代表更低 TTFT。

## 参考

- [controller 实现](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/lmcache/v1/distributed/storage_controllers/prefetch_controller.py)
- [设计说明](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/distributed/storage_controllers/prefetch_controller.md)
- [onboarding](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/docs/onboarding.md)
- [offload 流水](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/src/offload/pipeline.rs)
