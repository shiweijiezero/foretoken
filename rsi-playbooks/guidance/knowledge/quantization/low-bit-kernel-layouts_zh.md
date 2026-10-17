<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 将低位格式与可执行内核布局配套选择

[English](low-bit-kernel-layouts.md) | 简体中文

## 现象与动机

四位检查点可以省权重流量，却仍用十六位激活参与乘法。标签不能说明指令吞吐或每次前向的转换成本。格式选择必须结合 kernel 的实际操作数、scale 布局与形状。

## 原理与分析

Y=XWᵀ，X[M,K]、W[N,K]，块量化按 w≈s_block q 重建。MXFP4 每 32 个 E2M1 值共享 E8M0 二次幂 scale；NVFP4 每 16 个值共享 FP8 scale，并有外层 scale。不计补齐和外层 scale，存储分别为每权重 4.25 和 4.5 bit，并非恰好四位。

小 M 时每份权重服务少量 token，读取成本突出；大 M 复用权重，更依赖计算吞吐和激活转换。因此 W4A16 可改善 decode，却在大形状下落后于原生 FP8 或 W4A4。

## 设计

将检查点持久转换与每次前向分开：加载时按 backend 重排权重/scale，需要时才量化激活，再消费 tile、应用 scale 和输出处理。原生 W4A4 消费两种低位操作数；W4A16 保留高精度激活，在 tile 内反量化压缩权重。先物化完整高精度矩阵再做 GEMM 会损失压缩流量优势。

## 实现

执行步骤为：

1. 读取检查点量化元数据，确定可执行 kernel 与操作数类型。
2. 加载时转换、补齐、打包对应权重和 scale。
3. 每次前向按 kernel 需要准备激活 scale/量化 tile。
4. 执行分块 GEMM、scale 应用和输出处理，不物化完整反量化矩阵。

vLLM ModelOpt 自动选择可用原生 NVFP4，否则使用 Marlin W4A16。固定 MXFP4 实现已负责 backend 转换、补齐和 modular MoE dispatch。GPT-OSS MXFP4 保留普通线性层非量化，专家 FP4 不等于全模型 W4A4。SGLang 同样支持自动分派与显式覆盖。Blackwell 原生 NVFP4、AMD CDNA4 FP4 和旧设备 weight-only 的指令及布局要求不同。

## 影响与效果

压缩操作数减少常驻权重和读取，可为并发请求留出空间；scale 元数据、补齐、转换指令和激活量化会消耗部分节省。原生 kernel 可提升计算吞吐，weight-only 主要改善带宽。显存余量可能提高批处理吞吐，却不改善孤立请求延迟。4.25/4.5 bit 是格式存储推导，不是服务实测；最终执行取决于实际 kernel 和补齐后的形状。

## 参考

- [vLLM ModelOpt 格式与 backend 选择](https://docs.vllm.ai/en/latest/features/quantization/modelopt/)。
- [固定 commit 的 MXFP4 实现](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/quantization/mxfp4.py)。
- [SGLang 量化与 dispatch](https://docs.sglang.io/advanced_features/quantization.html)。
