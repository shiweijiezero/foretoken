<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用混合精度分配权衡质量、显存与内核成本

[English](mixed-precision-allocation.md) | 简体中文

## 现象与动机

统一低位给等大小层相同存储预算，却忽略质量敏感度差异。混合精度可在内存预算内保留质量，但平均 bit 数不能预测延迟，也不能保证融合算子能执行该组合。

## 原理与分析

ModelOpt AutoQuantize 以对角 Fisher 近似估计候选算子敏感度：

```text
S_i,f ∝ sum_k g_i,k² (Y_i,k − Y^Q_i,f,k)²
min sum_i S_i,f(i)，约束 sum_i N_i b_i,f(i) <= N_total b_target
```

Y 是参考输出，Y^Q 是 fake-quantized 输出，g 是评分损失对 Y 的梯度，分数汇总校准观察。N_i 为可量化权重数，b_i,f 是格式 bit 成本；排除的 embedding/norm 仍在预算外占内存。局部敏感度与对角曲率仅近似跨层误差作用，并未完整建模。

## 设计

每个部署组选择一种可执行格式，保持融合 QKV、gate/up 和专家的量化契约。组内敏感度与成本汇总后求整数约束分配。格式菜单必须符合 exporter/runtime；给 Q、K、V 独立选格式可能破坏融合投影。有效 bit 表示存储，不是阶段延迟；大 M 时原生 FP8 可能比四位 weight-only 更快。

## 实现

已有搜索流程为：

1. 捕获校准输入及评分损失的输出梯度。
2. 局部重放支持的候选量化器，累计敏感度。
3. 形成部署组和有效 bit 成本。
4. 在预算内求解每组一种格式的分配。
5. 按分配量化、导出，通过现有 runtime 格式路径加载。

引用 ModelOpt 快照提供梯度与 KL scoring。BF16、FP8、NVFP4、MXFP4 的实际菜单取决于硬件、模型排除项及 MoE 布局，不需另一套检查点协议。激活转换、补齐、输出处理和格式切换仍是组执行成本，即使 bit 目标未计入。

## 影响与效果

敏感度分配保护量化代价高的组，在其他组节约位数，同时增加校准前后向、候选重放和搜索。混合格式会改变 scratch、graph capture 与最大 batch，服务成本不能简单相加。更小方案可通过更大 batch 改善吞吐，却使低负载延迟变差。已有目标是在有效 bit 约束下优化质量，并未得到实测的全局延迟最优服务。

## 参考

- [AutoQuantize scoring、搜索、分组与限制](https://nvidia.github.io/Model-Optimizer/announcements/autoquantize.html)，ModelOpt 快照 `b5f10f8bf96201241c35b86498557bd6fdde10ca`。
- [vLLM 可执行 ModelOpt 格式](https://docs.vllm.ai/en/latest/features/quantization/modelopt/)。
- [SGLang 当前量化 backend](https://docs.sglang.io/advanced_features/quantization.html)。

另见[异构执行](../heterogeneous-hardware/)。
