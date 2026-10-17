<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少 KV 传输开销：布局、内存注册与 Connector 组合

[English](kv-transfer-layouts.md) | 简体中文

## 现象与动机

KV 搬运可能先受描述符构造、布局转换或完成通知限制，而不是 NIC 带宽。两端字节量相同也不代表缓存兼容：层、块、头、scale 和位置必须按一致语义解释。

## 原理与分析

串行延迟可估计为 `T = T_register + n T_descriptor + T_submit + D/B + T_completion + T_relayout`，其中 n 是描述符数量，D 是载荷字节，B 是有效字节/秒。其他项分别为注册、单个描述符构造、提交、完成处理和布局转换时间；发生重叠时，应计各项暴露在关键路径上的成本。64 层分别寻址的许多小范围，即使总载荷 D 不大，也可能需要很多描述符。合并连续范围或采用跨层物理布局可以减少 n；先 gather 到暂存张量则增加拷贝和内存。最优点取决于消息大小、局部性及 attention backend 是否接受该布局。

## 设计

选择 attention 与搬运两端都能接受的物理布局。连续或跨层范围减少描述符数量；把离散页 gather 到连续张量会增加拷贝与内存。稳定区域注册一次，其 owner 必须存活到全部相关操作完成。

兼容条件包括架构、dtype、attention 几何/backend 与传输模式。引用版本 NIXL 矩阵支持部分异构 TP。Mamba 要求相同 TP；异构块大小要求非混合模型、不使用 HMA 布局且生产者块更小。MLA 的复制 KV 不是普通分头。`LBHNC` 与 `LBNHC` 的头切分约束不同，跨层块要求 `BLHNC`。支持静态或 packed scale 不代表支持运行时动态 scale。

## 实现

Mooncake Transfer Engine 的一次操作如下：

1. 注册本地范围、打开远端 segment，将所选缓存块转换为源与目标范围。
2. 合并相邻且兼容的范围，分配 batch 句柄并提交描述符。
3. 等待 task/batch 完成后才发布目标就绪；在途期间保留两端分配。
4. 确认完成后才释放 batch 与内存保护，取消路径也遵循同一寿命边界。

vLLM `MultiConnector` 查询全部 connector：任一 lookup pending，组合仍 pending；否则选择第一个报告命中 token 的来源加载。向全部 connector 保存，并聚合异步完成。它不合并部分命中，也不动态选择最快源。LMCache MP 读取还执行 token scatter 与按 stream 顺序释放读取锁；Dynamo 物理传输层负责 executor／通知协作。

## 影响与效果

复用注册省去重复准备，合并范围减少 CPU 描述符构造与提交开销，尤其适合大量小范围；大载荷仍可能受带宽限制。跨层布局只有在 attention backend 接受时，才能省去相应打包。

Connector 组合扩大存储覆盖，也可能增加向全部后端保存的流量与 pending 查找等待。按完成顺序发布和回收，避免消费者读取半完成数据或搬运仍持有内存时复用缓冲区。载荷大小相同既不保证布局兼容，也不保证寿命正确。

## 参考

- [Transfer Engine 接口](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/mooncake-transfer-engine/include/transfer_engine.h)
- [NIXL 兼容矩阵](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)
- [worker 源码](https://github.com/vllm-project/vllm/tree/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/kv_transfer/kv_connector/v1/nixl)
- [MultiConnector 实现](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/kv_transfer/kv_connector/v1/multi_connector.py)
- [MP retrieve](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [物理传输层](https://github.com/ai-dynamo/dynamo/tree/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-physical/src/transfer)
