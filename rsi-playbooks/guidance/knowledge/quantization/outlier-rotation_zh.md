<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用坐标旋转分散离群值，降低量化误差

[English](outlier-rotation.md) | 简体中文

## 现象与动机

一个特别大的激活坐标就能决定整块 scale，让普通值缺少重建精度。旋转在量化前重新分散能量，其价值取决于能否保持模型计算，以及变换能否离线折叠，还是每次前向都执行。

## 原理与分析

行输入 Y=XWᵀ，正交矩阵 R 下令 X′=XR、W′=WR，则 X′W′ᵀ=XWᵀ。未量化线性映射不变，量化器却面对不同坐标。归一化 Hadamard 将 [a,0] 变为 [a/√2,a/√2]，降低最大值并保持范数。

全局、块内旋转与排列对每 16/32 个值共享 scale 的影响不同。任意旋转不能普遍穿过 SiLU 或门控乘积，跨过这些非线性移动变换会改变模型。

## 设计

激活变换与 consumer 权重配对，并保持残差、bias、norm、共享专家及 tied weights 的坐标一致。可兼容部分离线折叠，不能跨过非线性的部分保留在线变换，再重新校准。SpinQuant 对 embedding、attention/MLP 输入输出及 LM head 有具体重写，包含 down projection 的逆 Hadamard 处理；它不是所有 hybrid 模块的通用改写。

## 实现

核心准备流程为：

1. 收集通道/块激活统计，选择固定或学习的正交变换。
2. 一致地变换适用权重和相邻模型参数。
3. 插入必要在线变换，包括无法穿过非线性的部分。
4. 对变换后激活/权重重新求 scale，导出支持格式。

llm-compressor 列出 SpinQuant 和 QuIP，SpinQuant 提供学习旋转与 ExecuTorch 导出；vLLM 使用还需导出格式与运行时变换支持。TORQ 面向 MXFP4 构造 R=R_inter⊗R_intra：Givens rotation 均衡块间协方差对角项，块内旋转与 scale 更新交替，降低码字占用不均衡 sum_j(p_j−1/J)²。它仍在线旋转激活，将逆变换折叠进 consumer 权重；论文实现与已有 serving 集成是不同边界。

## 影响与效果

旋转可减少 clipping、改善码本利用，同时保持未量化映射。它增加离线模型变换和校准，无法折叠时还增加在线工作。小 decode batch 的变换耗时可能超过量化 GEMM 节省；密集变换也可能增大重排权重误差或破坏激活量化融合。TORQ 区分块间失衡与块内利用不足，SpinQuant 提供已有学习旋转路径。质量恢复可支持更小权重占用，即使变换增加延迟。

## 参考

- [SpinQuant 源码，commit 8f47aa3f00e8662caf1a484153920a07e5281c3a](https://github.com/facebookresearch/SpinQuant/blob/8f47aa3f00e8662caf1a484153920a07e5281c3a/eval_utils/rotation_utils.py)，提交于 2025-02-14。
- [当前 llm-compressor 方法](https://github.com/vllm-project/llm-compressor/tree/990fdd2fb41af48f105dbdebd088a96b627701b1)。
- [TORQ 方法、执行图与限制，v1](https://arxiv.org/abs/2605.19561)。

另见[Kernel 设计](../kernel-design/)。
