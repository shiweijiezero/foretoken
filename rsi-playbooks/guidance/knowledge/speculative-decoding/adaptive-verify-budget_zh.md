<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用自适应验证预算平衡推测收益与计算占用

[English](adaptive-verify-budget.md) | 简体中文

## 现象与动机

固定 draft 长度用更多单轮工作换取更少的目标解码轮次。在并发请求或长上下文下，即使接受率不变，额外验证也可能挤占服务容量。要优化的是单位服务占用时间提交的输出，而不是候选接受比例。

## 原理与分析

生成多少与验证多少是两个决策。自回归 draft 提前停止可省后续前向；单次并行 draft 已完成生成，截短只能省目标验证。设 a[r,j] 是前序全部接受时第 j 个候选的条件接受率，前缀存活率为 s[r,j] = product(a[r,1..j])，链式推测的近似目标为：

```text
sum_r (1 + sum_j<=K[r] s[r,j])
/ (draft时间 + verify时间(获准位置、上下文) + 校正提交时间)
```

K[r] 必须是连续前缀。额外的 1 表示校正或 bonus 输出，EOS 和输出上限会截断它。前序很难存活时，后面某个位置的高置信度没有独立价值。

## 设计

沿用已有 target/draft 配对，逐位置估计存活率，结合批次成本曲线选择获准前缀长度。目标只验证这些前缀，提交接受输出及校正/bonus，丢弃拒绝和未验证候选，下一轮从提交历史重建。分配成本包含 graph 填充与上下文相关验证成本：逻辑预算变小，不代表实际执行的图也变小。

## 实现

固定版本的 vLLM 对 DSpark 可使用训练置信头；其他 draft 使用由验证结果在线拟合的逻辑回归估计器。核心步骤是：

1. 生成候选，预测条件接受率。
2. 计算前缀存活率，按启动测得的单轮成本曲线选择长度。
3. 整理获准 query，执行目标验证。
4. 精确拒绝采样并提交有效输出，用实际到达位置的结果更新估计器。

该快照覆盖 EAGLE、MTP、DFlash、DSpark 和独立 draft，要求完整 CUDA Graph 及支持设备侧 query 长度的 backend，不支持 LoRA 和流水线并行。启动测量默认使用 8192 token 合成上下文。另一条 `num_speculative_tokens_per_batch_size` 策略可将 K 降为零，但不支持数据并行；它不是设备侧逐请求分配。

## 影响与效果

预算分配减少低存活率后缀的目标计算，把位置留给更有用的续写，同时增加估计、布局整理和状态记账。上下文成本过期或接受估计失准会造成错误分配，graph 向上取整也可能抵消位置节省。引用 RFC 报告了低负载未获益现象，其动机是饱和场景下的容量交换，不是通用加速倍数。循环模型的状态恢复以提交前缀为准，详见[回放缓冲](../attention-state/state-write-replay_zh.md)。

## 参考

- [固定提交的自适应验证文档](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/adaptive_verification.md)和[批大小策略文档](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/dynamic_speculative_decoding.md)界定该快照的支持范围。
- [PR #47808](https://github.com/vllm-project/vllm/pull/47808) 的置信调度验证于 2026-08-12 合并，提交为 `7f7a32cfec0f1bc5b73c37200b86631523a1ea8f`。
- [RFC #48202](https://github.com/vllm-project/vllm/issues/48202)：贡献者报告的低负载未获益结果及饱和场景动机。其中[PR #48692](https://github.com/vllm-project/vllm/pull/48692) 已关闭且未合并；当前支持以以上固定指南为准。
