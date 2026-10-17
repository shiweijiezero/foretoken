<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Match low-bit formats to executable kernel layouts

English | [简体中文](low-bit-kernel-layouts_zh.md)

## Observations and Motivation

A four-bit checkpoint can save weight traffic while still multiplying with sixteen-bit activations. Its label does not identify instruction throughput or per-forward conversion cost. Format choice must be paired with the kernel's actual operands, scale layout, and shape.

## Principles and Analysis

For Y=XWᵀ with X[M,K] and W[N,K], block quantization reconstructs w≈s_block q. MXFP4 uses E2M1 payloads and E8M0 power-of-two scales per 32 values; NVFP4 uses FP8 block scales per 16 values plus an outer scale. Ignoring padding and outer scales, storage is 4.25 and 4.5 bits per weight respectively, not exactly four.

At small M, weight traffic dominates because each tile serves few tokens. Larger M reuses weights and emphasizes arithmetic and activation conversion. W4A16 can therefore help decode but lose to native FP8 or W4A4 at larger shapes.

## Design

Separate persistent checkpoint conversion from per-forward work. Repack weights/scales once for the selected backend, quantize activations only where required, consume tiled operands, apply scales, and run the epilogue. Native W4A4 consumes both low-bit operands; W4A16 keeps activations high precision and dequantizes compressed weights inside tiles. Materializing the full high-precision matrix before GEMM undermines compressed weight traffic.

## Implementation

The execution path is:

1. Read checkpoint quantization metadata and determine supported kernel/operand types.
2. Convert, pad, and pack weights and scales for that backend during loading.
3. On each forward, prepare activation scales/quantized tiles if the kernel needs them.
4. Execute tiled GEMM with scale application and output epilogue, without full-matrix dequantization.

vLLM ModelOpt automatically selects native NVFP4 where available and a Marlin W4A16 path where native FP4 GEMM is unavailable. Its pinned MXFP4 implementation performs backend-specific conversion, padding, and modular MoE dispatch. GPT-OSS MXFP4 leaves ordinary linear layers unquantized; expert FP4 does not imply full-model W4A4. SGLang similarly offers automatic dispatch and explicit overrides. Blackwell native NVFP4, AMD CDNA4 FP4, and older-device weight-only paths have different instruction and layout requirements.

## Impact and Results

Compressed operands reduce resident weights and weight reads, potentially leaving more space for concurrent requests. Scale metadata, padding, conversion instructions, and activation quantization consume part of those savings. Native kernels can improve compute throughput, whereas weight-only execution principally targets bandwidth. More memory headroom can increase batched throughput without improving isolated decode latency. The derived 4.25/4.5-bit values describe ideal format storage, not measured service performance; selected kernels and actual padded shapes determine execution.

## References

- [vLLM ModelOpt formats and backend selection](https://docs.vllm.ai/en/latest/features/quantization/modelopt/).
- [vLLM MXFP4 implementation at the cited commit](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/quantization/mxfp4.py).
- [SGLang quantization and dispatch](https://docs.sglang.io/advanced_features/quantization.html).
