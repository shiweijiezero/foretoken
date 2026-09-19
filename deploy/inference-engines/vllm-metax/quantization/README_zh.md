<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 沐曦 INT8 权重存储

[English](README.md) | 简体中文

model-server 为 MACA 运行时构建镜像时安装此包。使用 `quantization: torchao` 和 `Int8WeightOnlyConfig`，完整配置见 [C500 示例](../../../../examples/quantized-model/torchao-metax/model.yaml)。

权重以 INT8 保存，执行每个线性层时反量化后进行浮点计算，降低的是权重存储占用，不是矩阵计算精度。此适配不支持量化 MoE 专家或其他 TorchAO 量化方案。

线性层覆盖针对 MACA PyTorch 2.10 与 TorchAO 0.15.0 中观测到的量化张量执行错误。升级任一依赖时，应在 C500 上比较原始模型和量化模型的生成结果，再决定是否移除覆盖。
