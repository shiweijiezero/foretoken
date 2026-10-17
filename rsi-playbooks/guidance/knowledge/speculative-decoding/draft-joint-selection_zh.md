<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 联合选择块内候选，改善草稿续写连贯性

[English](draft-joint-selection.md) | 简体中文

## 现象与动机

并行 draft 可生成各自合理、组合却矛盾的 token，例如开头定义一个标识符，后面引用另一个。目标验证会拒绝这种前缀，因此逐位置准确率高，实际提交跨度仍可能很短。

## 原理与分析

候选选择先受覆盖限制，再受排序影响：目标 token 不在 top-k 格中，selector 就无法找回。LiLiCorr 用小型双向 transformer 编码完整候选格，生成 incoming/outgoing 兼容向量，用相邻向量相似度补充一致性信息，不必为每个最终 token 串行执行神经前向。m 个位置、每位置 k 个候选产生 (m−1)k² 个相邻配对，还需候选格编码。扩大 k 提高覆盖，也带来二次增长的配对成本。

## 设计

使用一个目标、训练过的 DFlash backbone 和关联头。联合编码候选、批量计算配对得分，再从左向右按此前选择走出一条路径。正确性仍由目标因果验证承担。确定性选择对应点质量 proposal；随机选择须提供真实归一化条件分布，不能使用 backbone 的边际概率代替。DFlash2 使用前驱/后继 token embedding 与局部卷积，头权重和几何配置不能与 LiLiCorr 互换。

## 实现

核心流程是：

1. 从提交前缀和已验证 anchor 得到各 draft 位置的 top-k 候选。
2. 编码候选格，生成 incoming/outgoing 向量与相邻兼容得分。
3. 结合逐位置和兼容信息，选取或采样一条路径。
4. 因果验证这条链，精确接受与校正，只提交有效输出。
5. 丢弃其余路径和拒绝后缀。

论文用 oracle 前缀交叉熵及目标加权干扰候选惩罚联合训练 backbone 与关联头。引用的 SGLang 文档说明尚未发布 LiLiCorr draft，需先训练匹配权重。固定 vLLM 要求 `LiLiCorrDraftModel`、完整几何/头权重及 Model Runner V2；训练块 16 含 anchor，通常对应 15 个推测位置。更长长度没有训练 slot embedding。

## 影响与效果

联合选择改善候选组合不一致，而不是减少串行 draft 次数。候选格 attention、embedding、投影和配对工作都在目标验证前支付；覆盖改善不自动变成更长接受前缀。SpecForge 的 oracle top-k 和 covered accuracy 是固定记录块上的训练诊断，不是随机服务接受率。SGLang 在 `DFLASH` 下由检查点选择架构，主机长度同步或 head eager 回退会消耗连贯性收益。vLLM 文档仍将 LiLiCorr 专属块拒绝 GPU 验证及自适应验证组合标为待验证。缩短块使用已训练几何的前缀，并不生成新的位置权重。

## 参考

- [LiLiCorr 原论文](https://arxiv.org/html/2608.20530)：候选格、兼容向量、路径选择及训练目标。
- [vLLM 固定文档](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/lilicorr.md)：权重要求、长度约定、proposal 采样与待验证组合。
- [SGLang 固定指南](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx)：公开权重可用性及 graph/backend 条件。
- [SpecForge 训练指南](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md)：DFlash2 selector 目标与覆盖/排序诊断，和 LiLiCorr 训练配方区分。
