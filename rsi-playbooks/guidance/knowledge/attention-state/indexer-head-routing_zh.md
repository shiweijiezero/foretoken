<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按查询路由索引头，减少稀疏索引打分

[English](indexer-head-routing.md) | 简体中文

## 现象与动机

DeepSeek Sparse Attention（DSA）先扫描索引 key，再让昂贵的 MLA 读取选中 token。固定最终 top-k 限制了 MLA 工作，却暴露出 decode 的线性索引成本和完整 prefill 的平方级工作。MISA 先沿 head 轴减少打分，再选 token，作用于已训练的 DSA 模型，而不是替换主注意力内核。

## 原理与分析

设 query 为 `t`、历史 token 为 `s`、索引头为 `j`，各 head 共享索引 key `k(s)`，学习得到的 head 权重为 `w(t,j)`，DSA 计算：

```text
I(t,s) = sum_j w(t,j) * ReLU(q(t,j) · k(s))
selected(t) = topk_s I(t,s)
```

索引器先以聚合注意力为目标训练，再进行稀疏模型训练；DeepSeek-V3.2 报告每 query 选 2,048 个 token。这里的评分不是 MLA softmax。

MISA 将 key 按 `B` 个 token 求均值，估计各 head 的 affinity，从 `H` 个 head 中选择 `h` 个扫描历史。历史长度为 `L` 时，打分工作近似由 `H*L` 降为 `H*ceil(L/B)+h*L`。仍需读取全部历史索引 key，所以算术节省不等于同比例的流量节省。

## 设计

在写入索引缓存时维护池化 key，每个 query 独立选择 head。Prefill 的池化遵守各 query 的因果边界，decode 使用当前有效历史。

两阶段 MISA† 先用路由 head 得到较大候选集，再仅对候选使用全部 head 重排。论文示例为 8 个路由 head、1,024-token 池化块、8,192 个候选和 2,048 个最终 token。重排能改善候选内部的评分，却无法恢复初选遗漏的 token；均值池化可能掩盖孤立的高 affinity key。

## 实现

1. 对有效索引 key 池化，用全部 head 计算粗粒度 affinity。
2. 选取当前 query 的 head，仅用这些 head 对有效历史计算加权 ReLU 分数。
3. 直接选 token top-k，或先取较大候选集，在候选上重算完整 DSA 分数后再选最终 top-k。
4. 将选中的物理位置交给原有 sparse MLA，分别处理索引 key scale 和注意力缓存格式。

作者的一阶段、两阶段 patch 面向 vLLM 0.17.0、Transformers 5.2.0 和 TileLang 0.1.7.post3，两者只安装一种。`misa_ops.py` 与 `indexers.py` 承担路由和重排；按 query norm 路由是另一种 selector。引用的上游 vLLM indexer 已有量化、candidate-block mask 和分布式精确 top-k 合并；精确合并保留给定分数的排序，与近似 head 裁剪不同。

## 影响与效果

MISA 在 DeepSeek-V3.2、GLM-5 上无需重新训练。长上下文中，它减少大量 head-key 点积，但增加池化维护、head 路由和可能的重排。短上下文未必能摊薄这些阶段；打分缩短后，top-k 或 key 流量仍可能占主导。最终 token 集合可以不同于完整 head 的 DSA，尤其容易遗漏被均值掩盖的罕见 key。这种近似与 FP8/FP4 打分误差、注意力缓存量化是独立因素。上述参数描述选择工作，不代表所有服务负载的固定加速比。

## 参考

- [DeepSeek-V3.2 报告 v1](https://arxiv.org/html/2512.02556v1)：索引公式、训练方式、top-k 与 MLA 的关系。
- [MISA，2026-05-08，v1](https://arxiv.org/html/2605.07363v1)：池化 key 的 head 路由、重排、复杂度和经验保真度。
- [作者实现 README，固定 `dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc`](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/README.md)、[两阶段内核](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/misa_vllm_patch_2stage/misa_vllm_patch/misa_ops.py)与[indexer wrapper](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/misa_vllm_patch_2stage/misa_vllm_patch/indexers.py)：发布范围及实际路由/重排。
- [vLLM indexer，固定 `6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/sparse_attn_indexer.py)：该版本的评分、量化、top-k 和 candidate-block 路径。
