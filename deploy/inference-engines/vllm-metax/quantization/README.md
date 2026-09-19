<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# MetaX INT8 weight storage

English | [简体中文](README_zh.md)

The model-server build installs this package for MACA runtimes. Use `quantization: torchao` with `Int8WeightOnlyConfig` as shown in the [C500 example](../../../../examples/quantized-model/torchao-metax/model.yaml).

Weights remain INT8 between operations; each linear layer is dequantized for floating-point execution. This reduces stored weight memory, not GEMM precision. Quantized MoE experts and other TorchAO schemes are outside this adapter's scope.

The linear override addresses incorrect quantized-tensor dispatch observed with MACA PyTorch 2.10 and TorchAO 0.15.0. When updating either dependency, compare baseline and quantized generation on C500 before removing the override.
