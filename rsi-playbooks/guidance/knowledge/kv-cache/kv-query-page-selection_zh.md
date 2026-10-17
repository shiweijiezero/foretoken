<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过查询感知的页选择减少 KV 读取

[English](kv-query-page-selection.md) | 简体中文

## 现象与动机

少读 KV 不等于少存 KV。选择器可以保留全部信息，每次 query 读取不同页；淘汰方法则从活跃缓存中永久移除条目。这个区别决定后一问能否找回前一问没有使用的信息。

## 原理与分析

[Quest，2024 年 6 月](https://arxiv.org/html/2406.10774v1)保留完整缓存，为每页每个 key 维度保存最小值和最大值。给定 query `q`，页 p 的分数上界为：

`U_p(q) = sum_i max(q_i min_p(k_i), q_i max_p(k_i))`。

页内任意 key 与 q 的点积都不会超过这个逐维上界。但各维极值可能来自不同 token，因此上界可以很松；按上界排序不等于精确找 top token，也不证明被省略概率质量很小。

页大小 S、上下文 N、选中 K 页时，相对完整 K/V 扫描的元数据和 KV 流量可近似为 `1/S + KS/N`，假设元素宽度相同，暂不计选择与工作区流量。例如 S=16、N=32768、K=128 时约为 12.5%，不能据此预测服务快 8 倍，页打分、top-k、启动与非 attention 层仍存在。

## 设计

保留完整分页 K/V，并为每个 key 页保存紧凑的 min/max 摘要；新 token 到达时更新摘要。每次 decode query 重新选择页，attention 内核再读取这些页的完整 K/V。选择属于 attention 执行职责，缓存存储提供页几何，卸载时还负责取回数据。

Quest 论文配置保留前两层稠密执行，因为这些层的有效稀疏性较弱。页预算约束读取流量，不约束缓存保留容量。多事实聚合通常比单 passkey 需要更多页；把未选中页移出 HBM，会新增加载依赖。

## 实现

对每个稀疏层与 attention head 执行：

1. 追加新 key/value，更新所在页逐维最小值和最大值。
2. 按 query `q` 为每页计算上界：`q_i >= 0` 使用最大 key 分量，`q_i < 0` 使用最小分量。
3. 按预算选择 top-k 页 ID，收集有效 token 位置，仅读取对应 K/V 范围。
4. 计算 query-key 分数，应用原有因果 mask，在选中 token 上归一化并累积 value。

输出是在查询相关子集上的近似 attention，不是得到证明的精确 attention。Quest 原始内核实现打分与选择；vLLM 分页几何、LMCache 稀疏片段读取、Dynamo KVBM 与 Mooncake 搬运提供的是缓存基础设施，而非这一打分算法。

## 影响与效果

在 32K 上下文、2,048 token 预算下，Quest 在 RTX 4090、CUDA 12.2 上为 Llama2-7B 测得相对完整 FlashInfer attention 的 7.03 倍自注意力加速。同样上下文与预算的 Ada 6000 端到端实验中，FP16 权重相对 FlashInfer 加速 1.74 倍，4-bit 权重加速 2.23 倍。内核与端到端实验使用不同 GPU，不能合并成同一组比较。

收益来自避免读取大部分完整缓存。元数据打分、top-k 与非 attention 层仍有成本；全部 K/V 都被保留，因此方法本身不减少 HBM 容量。每次 query 重新选择，之前未选中的页仍可服务后续问题。

## 参考

- [Quest，2024 年 6 月](https://arxiv.org/html/2406.10774v1)
- [NIXL 兼容说明](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)
- [MP blend](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [KVBM onboarding](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/docs/onboarding.md)
- [Transfer Engine](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/mooncake-transfer-engine/include/transfer_engine.h)
