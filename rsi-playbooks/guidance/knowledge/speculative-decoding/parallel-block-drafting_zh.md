<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用并行块预测消除串行草稿前向

[English](parallel-block-drafting.md) | 简体中文

## 现象与动机

自回归 draft 提议 K 枚 token，可能需要 K 次串行辅助前向，每次支付启动、attention 和输出投影成本。目标模型更快也不会消除这条串行链。并行块预测用一次块前向替代逐 token 前向。

## 原理与分析

PARD 将小自回归模型适配为未来位置 mask 预测，只使用 token 上下文，不读取目标 hidden 特征。DFlash 是独立训练、依赖目标特征的块扩散 draft：已验证 anchor 引出 masked 块，块内双向交互，一次前向完成预测。目标验证仍是因果的；各位置单独合理，不代表组合连贯。

在提交长度相近时，成本由 K×draft_forward + verify(K) + correction 变为 block_forward(K) + feature_handling + verify(K) + correction。块宽仍会增加验证行数、投影与缓冲。

## 设计

使用一个目标加一个辅助模型。DFlash 拼接目标所选层特征，投影、归一化后作为 draft 的额外 key/value；PARD 则构造 token 级 masked 输入。目标因果验证线性续写，只提交接受前缀和校正/bonus，并用提交 token/特征延伸 draft 上下文。单次 draft 完成后截短验证只省目标工作，不能收回生成成本，详见[自适应预算](adaptive-verify-budget_zh.md)。

## 实现

DFlash 核心流程为：

1. 冻结目标，生成训练响应，选择干净 anchor 并 mask 未来位置。
2. 用块内双向 attention 和分离的训练块学习单次预测；提高早位置权重，因为首错使后续候选失效。
3. 加载目标专属匹配权重，在 prefill/提交后取得特征。
4. 一次预测 masked 块，因果验证，再丢弃未提交后缀。

PARD 使用未来 mask 目标和条件丢 token 适配。vLLM 文档提供 `amd/PARD-Qwen3-0.6B` 与 `parallel_drafting: true`；DFlash 发布目标专属权重。已合并 DFlash2 路径也需匹配检查点。固定 SGLang 可推断块大小，要求 draft window 至少覆盖块，不支持流水线并行和 DP attention，并关闭 overlap scheduler 与 mixed chunked prefill。块大小约定可能包含 anchor。随机验证必须取得真实 proposal 分布。

## 影响与效果

DFlash 论文在 NVIDIA H200、Transformers backend 上报告：Qwen3-8B 的 GSM8K 解码相对普通自回归达到 5.15 倍，表 1 的平均接受长度 τ=6.54 token，包含目标 bonus token。条件为关闭 thinking、温度 0、最多输出 2048 token、五层 draft 和块大小 16。
并行生成消除串行辅助启动依赖，同时增加特征处理和宽块工作。早期拒绝浪费目标行，更大块未必带来更多提交输出；调度功能排除也是部署成本。[联合选择](draft-joint-selection_zh.md)另行解决连贯性。

## 参考

- [PARD v4](https://arxiv.org/html/2504.18583v4)：并行 mask、目标独立性及适配训练；[vLLM 固定提交示例](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/parallel_draft_model.md)。
- [DFlash 原论文](https://arxiv.org/html/2602.06036)：特征条件单步 draft 与训练。[官方仓库](https://github.com/z-lab/dflash)在 `07ebd93db9f472af339b644bb70221ad8428328a` 列出公开模型合集。
- [vLLM #52816](https://github.com/vllm-project/vllm/pull/52816) 于 2026-08-21 合并为 `b389ac29465b33f9e9c534df221ea3c129e9793f`；[SGLang #35371](https://github.com/sgl-project/sglang/pull/35371) 于 2026-08-19 合并为 `c14312a66420b75ca9a11bf1817c4db1fa26b097`。
- [SGLang 固定文档](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx)及[SpecForge 训练指南](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md)说明推理与训练边界。
