<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用自反馈训练改善多步草稿预测

[English](draft-rollout-training.md) | 简体中文

## 现象与动机

只在已验证目标特征上训练的 draft，在推测后续位置时会改用自己的 hidden output。这种输入变化可能使教师强制准确率很高的模块，在真实多步续写中迅速失效。自反馈训练针对的是这项偏差，不是单纯增大辅助模型。

## 原理与分析

EAGLE-3 融合目标低、中、高层特征，直接预测 token，不再采用早期 EAGLE 的末层特征回归。未验证未来位置没有对应目标特征，draft 便反馈自身 hidden output。训练中模拟这条路径，使模块接触实际会产生的输入。若 a[j] 是前序均接受时第 j 位置的接受率，期望接受链长为 sum_j product_i<=j a[i]；误差会累积，因此首位置准确率不能代表多步表现。

## 设计

区分候选来源：独立 draft 是自己处理前缀的第二个 token 模型；EAGLE-3 是独立训练、依赖目标特征的模块，可沿模型专属路径复用 embedding/投影；原生 MTP 使用目标架构训练好的预测层，通常无需另供检查点，但部分 assistant 路径仍需独立权重。重复调用一个头不等于多个分别训练的未来位置头。它们都遵循生成、验证、提交的外层职责，拒绝候选不能成为下一轮提交历史。

## 实现

EAGLE-3 的训练与使用步骤为：

1. 生成目标响应，记录所选特征层，并保持 tokenizer、chat template、位置与 loss mask 一致。
2. 在干净位置融合目标特征和 token embedding。
3. 用 draft 自身 hidden output 展开多步预测，在这些自反馈输入上优化 token 目标。
4. 导出与目标匹配的辅助检查点。
5. 推理时生成链/树，由目标验证因果路径，再按提交 token 和已验证特征重建历史。

固定 SpecForge 提供 `specforge train --config ...` 及 EAGLE3 在线/离线配方。离线读取已记录特征；在线目标推理由 SGLang 捕获服务负责，而不是 trainer。vLLM/SGLang 现有路径加载匹配 EAGLE3 或受支持的 MTP 权重；新目标没有匹配权重时需要训练。

## 影响与效果

自反馈训练减少后续位置的输入偏差，可改善前缀存活，且不把训练操作加入推理。然而响应生成、特征捕获、训练、辅助权重驻留与 draft 前向仍有成本。离线特征省去重复目标推理，也固定了数据分布；更新目标、量化或 backend 可能改变对齐。目标自身生成的响应更贴近实际续写行为。领域覆盖和 BOS/template 格式也会独立影响接受情况；引用的 vLLM preamble 修复为支持的 benchmark 数据集提供 `--skip-chat-template`。

## 参考

- [EAGLE-3 原论文](https://arxiv.org/html/2503.01840)：多层融合、去除特征回归和训练时自反馈机制。
- [固定提交的 SpecForge 训练指南](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md)及[README](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/README.md)说明 EAGLE3 捕获、训练配方与指标。
- [vLLM EAGLE](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/eagle.md)、[MTP 指南](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/mtp.md)及[SGLang 文档源码](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx)界定当前推理入口。
- [vLLM PR #25916](https://github.com/vllm-project/vllm/pull/25916) 于 2025-10-02 合并，提交 `1cab2f9cad22807150c93b4b5635ff884884ca5a`；preamble 修复为支持的 benchmark 数据集增加 `--skip-chat-template`。
