<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance MoE traffic and error through quantized layouts

English | [简体中文](moe-quantized-layouts_zh.md)

## Observations and Motivation

An FP4 or FP8 label does not define an executable MoE representation. Packed values, scale encoding and granularity, matrix orientation, router weighting, and final reduction affect both speed and answers. FlashInfer's quantized paths make these choices explicit so reduced traffic does not come from misinterpreting operands.

## Principles and Analysis

Four-bit values pack two elements per byte, but true traffic includes scales and alignment. NVFP4 scales blocks of 16 elements; MXFP8 uses blocks of 32 with a different scale representation. With block size `b` and `s` scale bytes, storage per element includes `s/b` beyond the packed value bytes. Scale placement matters as much as array length.

Reduction precision is independent of weight precision. BF16 transported partials summed in FP32 with one final BF16 cast differ from unordered BF16 atomics. Quantizing FC2 partials adds another rounding step before reduction. For terms `a` and `-a+delta`, the final result is small while individual rounding errors scale with the large terms; relative error against the near-zero sum alone obscures that cause. Moving router weighting across a low-precision operation also changes rounding.

## Design

Prepare immutable weights and scales once, then choose per-forward activation and transport formats together with their consumers. Pre-dispatch quantization can reduce communication; post-dispatch quantization avoids requiring the transport backend to interpret that format. FC1, activation, FC2 input preparation, and combine each retain explicit precision and scale layouts.

Preserve gate/up row pairing through orientation changes and padding. The cited TRT-LLM FP4 routed-GEMM API accepts linear hidden-state scale layouts and rejects swizzled ones. Expert replication or migration carries scales with weights as one representation.

## Implementation

1. Load quantized weights and scales from the chosen checkpoint; convert orientation, packing, and gate/up layout to the backend's expected representation before graph capture.
2. Apply the model's original router scoring and normalization. Quantize hidden states before or after dispatch according to the supported producer-consumer path.
3. Execute FC1 with matching activation/weight scales, apply the gated activation, and prepare FC2 inputs in its required format.
4. Execute FC2, apply routing weights at the specified stage, and return partials in the chosen wire format.
5. Reduce with the configured accumulation precision and cast the final output once where that path specifies it.

FlashInfer exposes NVFP4, MXFP8, and FP8/FP4 DeepGEMM paths. SM100-family NVFP4 support does not imply native Hopper FP4 or MetaX support. W4A16, W4A8, and W4A4 differ in activation precision and conversion work; some split W4A8 paths require feature alignment to 128, not every MoE backend.

## Impact and Results

Lower-bit weights reduce weight traffic, and quantized transport can reduce payloads, but both add scaling, packing, and conversion. A smaller wire format can require a less favorable token-return schedule, especially at small batch; [megakernel partitioning](small-batch-megakernels.md) explains that interaction.

Agreement with dequantize-plus-FP32 computation on the same quantized operands establishes layout interpretation, not equivalence to the original checkpoint. Quantization quality and reduction error are separate effects. Additional workspace can also displace KV capacity. Format selection therefore changes traffic, numerical error, and execution eligibility together, rather than supplying a universal speedup from bit width alone.

## References

FlashInfer commit [a8e747c3](https://github.com/flashinfer-ai/flashinfer/tree/a8e747c3c1f7dfe5d51fab3883824b15da6829b7), [MoE-EP architecture](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/docs/design_docs/moe_ep_architecture.md), its SM100 tuning notes, and the [TRT-LLM FP4 API](https://docs.flashinfer.ai/generated/flashinfer.fused_moe.trtllm_fp4_block_scale_moe.html).
