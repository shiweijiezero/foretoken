<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 复用后缀续写生成候选，省去神经草稿计算

[English](suffix-reuse-drafting.md) | 简体中文

## 现象与动机

代码编辑、重复 agent 回合和 rollout 经常续写 prompt 或历史输出中已有的片段。搜索这些 token 序列可省去神经 draft 前向。它不同于前缀缓存：后者复用已知输入的计算，后缀推测预测未知输出。

## 原理与分析

Suffix Decoding 用当前末尾 token 匹配历史，按续写频率/路径得分排序，并调整提议深度。只需要一个目标，无辅助神经模型或新训练权重。频率估计用途，但确定性选择的 q 仍是点质量；若目标概率为 p(x)，精确逐 token 验证以 p(x) 接受，拒绝时从去掉 x 的目标分布采样。只有真正按频率随机提议，才用归一化计数作为 q。

## 设计

原报告在 CPU 上维护先前输出的全局后缀树，以及当前 prompt/生成的逐请求树，候选可组成紧凑树。实际验证接收树还是链，由引擎 adapter 决定。共享历史限定在允许的数据范围和保留规则内：精确采样不授权跨租户保存。索引只更新已提交输出；拒绝候选和取消的推测工作不是目标真实生成的观察。

## 实现

一次完整流程是：

1. 在请求本地及允许的共享索引中，查找提交后缀的匹配。
2. 提取续写，用条件频率评价路径，并按匹配强度和配置上限限制深度。
3. 返回实际候选布局；随机提议同时返回真实归一化 proposal 分布。
4. 交给已有因果目标验证及校正采样器。
5. 提交接受路径/前缀及校正/bonus，只把这些 token 加入索引，下轮查询新的提交后缀。

固定 vLLM 通过 `arctic-inference` 集成此方法，`num_speculative_tokens` 是上限而不是固定逐请求长度，也支持 prompt n-gram。索引职责及返回布局/生命周期由安装的 Arctic Inference adapter 承担。点质量规则详见[采样与校正](correction-fused-sampling_zh.md)。

## 影响与效果

复用省去辅助模型驻留、训练和神经草稿计算，增加查询、提取、主机设备传递、索引更新及目标验证。长重复片段可提供有用候选；匹配后一个标识符变化就可能使多数目标行白算。新颖文本、高熵采样及多义续写缩短有效跨度。更大历史增加潜在覆盖，也增加内存、更新和查询尾部成本；热共享历史与请求本地 prompt 复用拥有的证据不同。

## 参考

- [Suffix Decoding 原报告](https://arxiv.org/html/2411.04975)：逐请求/全局树、续写频率和自适应扩展（2024）。
- [vLLM 固定提交指南](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/suffix.md)：集成、依赖和最大候选长度含义。
- [Arctic Inference](https://github.com/snowflakedb/ArcticInference) 负责历史索引；返回布局与索引生命周期以安装的 adapter 版本为准。
- [vLLM 固定采样器](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/sample/rejection_sampler.py)：点质量提议和残差校正。
