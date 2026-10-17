<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按量化缓存布局匹配 MLA 解码内核

[English](mla-layout-matching.md) | 简体中文

## 现象与动机

Multi-head Latent Attention（MLA）将历史压缩为多个 query head 共享的 latent，但解码仍要扫描缓存并合并分片输出。缓存变小后，scale 读取、离散 gather、反量化和投影可能成为主要成本。写入方与注意力内核必须使用同一模型的物理格式；错误解释更小的缓存并不是优化。

## 原理与分析

设 latent 为 `c_t`、位置 key 为 `r_t`，吸收投影后，一个 head 的简化计算为：

```text
score(t) = q_latent · c_t + q_position · r_t
z        = sum_t softmax(score(t)) * c_t
output   = project(z)
```

缓存并非每个 head 展开后的 K/V。投影吸收改变内核维度，但仍须保留模型投影和位置规则。稀疏选择改变参与计算的 token，量化改变这些 token 的数值表示。

split attention 为每片产生归一化输出 `o_i` 和 log-normalizer `L_i`。合并应计算 `L=logsumexp_i(L_i)`、`o=sum_i exp(L_i-L)*o_i`，不能直接平均。

## 设计

布局需要与模型和硬件一起选择。早期 FlashMLA 的 V3.2 稀疏格式包含 512 字节 E4M3 latent、四个 FP32 scale、64 个 BF16 RoPE 值，共 656 字节/token；每个 scale 覆盖 128 个 latent 值。

V4.1 的 512 个 FP8 值已包含 64 个 RoPE 维度，加十六个 E8M0 scale，共 528 字节。可选 FP4 `extra_k_cache` 将 512 个 E2M1 值打包为 256 字节，加 32 个 E4M3 scale，共 288 字节，与 FP8 主缓存配合。V4.0 的维度和 page-scale 位置另有规定。这些是不同格式，不是可以直接互换的压缩档位。

## 实现

1. 按模型布局写入缓存行和 scale，保留位置分量及 page 寻址关系。
2. 为当前长度、top-k、query 数和 head 配置准备物理 indices 与 `get_mla_metadata`。
3. `flash_mla_with_kvcache` 收集缓存行，解释 scale，计算 latent/位置分数并累计 latent 加权的分片输出。
4. 按 log-normalizer 合并 split，再执行模型输出投影。重排 gather 时保持 indices、数值与位置特征的对应关系。

2026-09-30 的 breaking change 将 V3/V3.2/V4.0 和 Hopper 用户指向引用的旧版本；早期稀疏路径覆盖 SM90/SM100。当前 V4.1 文档覆盖 SM100/SM103 与 Ascend 950，decode 仅接受量化缓存，软件要求也不同。选择 token 的成本占主导时，见[索引头路由](indexer-head-routing_zh.md)。

## 影响与效果

656、528、288 字节描述存储格式，不是加速比：它们属于不同模型布局，288 字节格式还是额外缓存而非主缓存替代。量化减少数值流量，同时增加 scale 读取与解码；稀疏 indices 分散在远处 page 时，访存效率仍可能很低。长上下文且受缓存带宽限制的 decode 更容易受益，小批量则可能仍受启动开销限制。元数据、split 归约、缓存写入和投影仍构成完整模型的延迟。

## 参考

- [FlashMLA 当前 README，固定 `2e5429fc5653bab6e081f09477126f731882a6a9`](https://github.com/deepseek-ai/FlashMLA/blob/2e5429fc5653bab6e081f09477126f731882a6a9/README.md)：V4.1 格式、硬件范围、API 与 9 月 30 日 breaking change。
- [明确指定的旧 revision `ba89a3466e9470ad08ab39738d4e7bb66989e1e7`](https://github.com/deepseek-ai/FlashMLA/blob/ba89a3466e9470ad08ab39738d4e7bb66989e1e7/README.md)：V3.2/V4.0 布局与早期稠密/稀疏 decode 约定。
- [DeepSeek-V3.2 报告 v1](https://arxiv.org/html/2512.02556v1)：DSA 使用 MLA MQA 的方式及模型级稀疏选择。
