<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 用量化布局权衡 MoE 流量与数值误差

[English](moe-quantized-layouts.md) | 简体中文

## 现象与动机

FP4 或 FP8 标签不能定义可执行的 MoE 表示。打包值、scale 编码与粒度、矩阵方向、路由加权和最终归约同时影响速度与答案。FlashInfer 的量化路径明确这些选择，避免以错误解释操作数换取更低流量。

## 原理与分析

四位值每字节存两个元素，但真实流量还包括 scale 和对齐。NVFP4 每 16 个元素一块，MXFP8 每 32 个一块，scale 表示也不同。块长为 `b`、每块 scale 占 `s` 字节时，每元素除打包数值外还分摊 `s/b` 字节。Scale 排列与元素总数同样重要。

归约精度独立于权重精度：传输 BF16 partial、用 FP32 求和后一次转 BF16，与无序 BF16 原子累加不同。量化 FC2 partial 在归约前又增加一次舍入。对于 `a`、`-a+delta`，最终和很小，但各项舍入误差随大项增长；只看近零结果的相对误差会掩盖原因。路由加权跨越低精度操作时，舍入也会改变。

## 设计

权重和 scale 一次准备，每步激活与传输格式则和消费方一起选择。Dispatch 前量化可降低通信量；dispatch 后量化可避免要求通信后端解释该格式。FC1、激活、FC2 输入准备和 combine 分别保持明确的精度与 scale 布局。

矩阵转向和 padding 都须保留 gate/up 行配对。引用的 TRT-LLM FP4 routed-GEMM API 接受线性 hidden-state scale 布局，拒绝 swizzled 布局。专家复制和迁移将 scale 与权重作为同一表示处理。

## 实现

1. 从所选 checkpoint 加载量化权重和 scale，在图捕获前转换为后端要求的方向、打包及 gate/up 布局。
2. 使用模型原始路由打分和归一化，按支持的生产者—消费者路径在 dispatch 前或后量化 hidden states。
3. FC1 使用匹配的激活/权重 scale，执行门控激活，再准备 FC2 所需输入格式。
4. 执行 FC2，在指定阶段应用路由权重，并以所选线上格式返回 partial。
5. 按配置的累加精度归约，在相应路径规定的位置转换最终输出。

FlashInfer 提供 NVFP4、MXFP8 和 FP8/FP4 DeepGEMM 路径。SM100 系列 NVFP4 支持不代表 Hopper 原生 FP4 或沐曦支持。W4A16、W4A8、W4A4 的激活精度和转换工作不同；部分分离式 W4A8 要求特征维度对齐 128，不是所有 MoE 后端的共同限制。

## 影响与效果

低位权重减少权重读取，量化传输减少 payload，但增加缩放、打包和转换。小批量中，更小线上格式还可能需要更不利的返回调度，见[megakernel 分工](small-batch-megakernels_zh.md)。

与相同量化操作数的反量化加 FP32 计算一致，只能说明布局解释正确，不说明与原 checkpoint 等价；量化质量和归约误差是不同影响。额外工作区也可能挤占 KV 容量。因此格式选择同时改变流量、数值误差与执行资格，不能仅凭位宽给出通用加速比。

## 参考

FlashInfer [commit a8e747c3](https://github.com/flashinfer-ai/flashinfer/tree/a8e747c3c1f7dfe5d51fab3883824b15da6829b7) 的 [MoE-EP 架构](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/docs/design_docs/moe_ep_architecture.md)、SM100 调优笔记与 [TRT-LLM FP4 接口](https://docs.flashinfer.ai/generated/flashinfer.fused_moe.trtllm_fp4_block_scale_moe.html)。
