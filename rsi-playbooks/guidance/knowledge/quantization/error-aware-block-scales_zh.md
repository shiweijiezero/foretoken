<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 根据激活统计选择块缩放，降低量化输出误差

[English](error-aware-block-scales.md) | 简体中文

## 现象与动机

两块权重即使重建误差相同，也可能产生不同模型误差，因为负载使用的输入方向不同。按最大值选择 scale 不包含这项信息。激活感知校准改变 scale 选择，同时保留普通 NVFP4 推理表示。

## 原理与分析

设 Y=WX，W[N,K]，校准输入 X[K,T]，Δ=Q(W,s)−W。输出重建目标为：

```text
E = ||ΔX||²_F = tr(ΔHΔᵀ)，H = XXᵀ
```

H 是未中心化二阶矩，此处常称 local Hessian。对角项强调经常激发的通道，非对角项表示相关输入上误差的叠加或抵消。固定归一化不改变最优解。NVFP4 的 E2M1 间距不均匀，也使最大值缩放并非总合适：Four Over Six 比较把块最大值放在 4 或 6 附近，不改变四位 payload。

## 设计

ModelOpt Local-Hessian 将输入轴按 16 个值分块，每块记录 16×16 二阶矩，以 Δ_b H_b Δ_bᵀ 搜索可表示 FP8 scale。官方列出 126 个正且非零候选。各块独立让搜索可行，也忽略跨块误差相互作用。逐层校准可让后续层消费前面 fake-quantized 输出，反映传播，而不是假设全部输入保持 BF16。

## 实现

离线算法为：

1. 收集可量化 dense/MoE 线性层的校准激活。
2. 对每个输入块累计 H_b = X_b X_bᵀ。
3. 枚举可表示 scale，量化并重建权重，计算 Δ_b H_b Δ_bᵀ。
4. 保留最小得分 scale，导出普通 NVFP4 权重与元数据。

Four Over Six 则重建 max-to-4 和 max-to-6 两种候选，按重建误差选择；仓库默认 MSE，提供 Blackwell CUDA/Triton 和 PyTorch 参考。相同 4/6 选择不能普遍迁移到 MXFP4，因为二次幂 E8M0 无法表示所需 1.5 倍变化。ModelOpt 已在校准路径提供 Local-Hessian。部署使用最终格式的普通 kernel，无需新的 Local-Hessian kernel。

## 影响与效果

激活感知目标降低校准所覆盖方向的误差，不再把全部权重误差同等计价。搜索增加离线统计和重建工作，不增加运行时算子或检查点类型。Four Over Six 则重新分配浮点码本中的精度。两者都不能补回未观察到的激活：稀有 MoE 专家和迁移领域可能统计不足。块局部目标也未建模跨块相关误差。因此可以改善质量而保持 GEMM 执行不变；候选数量是算法参数，不是性能测量。

## 参考

ModelOpt 快照 `b5f10f8bf96201241c35b86498557bd6fdde10ca`。

- [Local-Hessian 方法、接口与限制](https://nvidia.github.io/Model-Optimizer/announcements/local-hessian.html)。
- [校准实现](https://github.com/NVIDIA/Model-Optimizer/blob/b5f10f8bf96201241c35b86498557bd6fdde10ca/modelopt/torch/quantization/model_calib.py)。
- [Four Over Six 论文，arXiv 2512.02010v5](https://arxiv.org/abs/2512.02010)与[作者实现](https://github.com/mit-han-lab/fouroversix)。作者实现版本为 `dadfad6901d473a734fe71e0b082e70ee993e23a`（2026-04-21）。
