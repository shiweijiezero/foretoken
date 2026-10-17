<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用对齐的状态检查点复用混合模型前缀

[English](hybrid-prefix-checkpoints.md) | 简体中文

## 现象与动机

混合模型不能仅靠前缀 attention KV 恢复：循环矩阵和卷积缓冲必须对应完全相同的已处理 token。如果两个请求更早分叉，上一请求的最终状态就不能用于当前续写。对齐检查点使共同边界可复用，而不必每个 token 都保存循环快照。

## 原理与分析

两个请求共享 token 0–89，attention KV 覆盖 90 个 token，但循环检查点仅在 64 和 128 个 token 后存在。128 的状态已含不同后缀；应从 64 恢复，重放 token 64–89。

可用缓存覆盖是 attention 组、循环层与卷积状态的交集。检查点间隔为 `C` 时，从有效的前置检查点到命中末尾最多需重放 `C-1` 个 token。更小间隔减少重算，但增加快照和复制；decode 状态大小固定，并不意味着快照便宜。

物理分配还有另一约束：vLLM 将兼容层组织为等大小 page。大循环矩阵可能迫使 attention block 扩大或增加 padding。因此物理 page 字节和逻辑 token 间隔分别影响容量与命中粒度。

## 设计

发布按已处理 token 边界标识的不可变快照。查询选择所有必需组都能提供的恢复边界，每个续写通过 copy-on-write 获得可写循环状态。保留重放区间所需 attention 覆盖，最终让各组在同一位置恢复。

发布、传输与可写状态是不同生命周期阶段。Connector 除 attention page 外还须传输循环、卷积状态，并在使用前完成传输；cache manager 负责协调，router affinity 不能补出缺失状态。

## 实现

1. 分别匹配各缓存组的请求前缀，找到共同可恢复的检查点边界。
2. 取得 attention page 和对应循环/卷积快照，在修改前完成待执行传输与 copy-on-write。
3. 重放检查点到共享前缀末尾的 token，保持各层同步，再执行分叉后缀。
4. 在允许导出的 scheduler 边界发布快照，由既有 manager 保留或驱逐状态 block。

固定版本 `MambaManager` 实现对齐查询、内部检查点、partial-tail 缓存及待执行复制。vLLM v0.30.0 的细粒度匹配要求 `mamba-cache-mode=align`、Mamba group 上的 EAGLE/MTP，以及显式设置小于 Mamba block 且整除相关 block size 的 `prefix-match-unit`；文档排除 multi-module MTP。这些前提属于该版本的细粒度模式，不是所有 hybrid 前缀复用的共同要求。

## 影响与效果

对齐复用避免重算整个共享前缀；90-token 示例从有效的 64-token 快照恢复，只剩 26 个 token 要重放。这是边界计算，不是实测延迟。更细检查点节省重放，但增加快照显存、复制，并可能挤占 KV 容量。重复系统 prompt 和多轮续写能提供可复用边界，无关流量主要占用保留空间。Copy-on-write 保持分叉续写独立，共同边界规则则避免仅命中 attention 时恢复错误的循环历史。

## 参考

- [vLLM v0.30.0 automatic prefix caching](https://docs.vllm.ai/en/v0.30.0/features/automatic_prefix_caching/)：细粒度选项及明确前提。
- [vLLM hybrid cache 设计](https://docs.vllm.ai/en/latest/design/hybrid_kv_cache_manager.html)：共享 page、分组、前缀交集与文档中的实验性限制。
- [`MambaManager`，固定 `6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/single_type_kv_cache_manager.py)：该版本的对齐/细粒度查询、检查点保留和 copy-on-write。
