<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Recover quantized quality through rounding and scale learning

English | [简体中文](quality-rounding-scales_zh.md)

## Observations and Motivation

Round-to-nearest minimizes immediate scalar error, not transformer-block output error. When direct calibration loses quality, offline optimization of rounding or scales can recover accuracy while retaining an existing low-bit deployment format. This differs from retraining all model weights.

## Principles and Analysis

For reference block F_l, fake-quantized block F̂_l, and calibration hidden states h, use L_block=sum_h ||F̂_l(h;θ)−F_l(h)||². Integer rounding can choose neighboring levels; FP4's uneven levels require the actual floating-point fake quantizer. Scale/clipping also changes the interval containing each value. Optimizing continuous scales still ends with representable exported FP8 scales, so export projection is part of the numerical behavior.

## Design

AutoRound captures block inputs/reference outputs and optimizes rounding/clipping before advancing through decoder layers. Scale learning instead trains NVFP4 per-block weight scales represented by `amax`, using fake quantization and straight-through estimators. It supports scale-only or joint weight/scale learning, with MSE initialization and distillation-style KL examples. Sequential reconstruction approximates cross-layer interactions; scale-only training cannot freely move individual weights. MoE calibration needs actual routed tokens to provide expert evidence.

## Implementation

The offline flow is:

1. Capture calibration hidden states and reference block outputs.
2. Initialize the actual format's quantizer/scales.
3. Optimize AutoRound's rounding/clipping variables `V`, `α`, `β`, or optimize per-block `amax` scales with straight-through gradients.
4. Freeze choices, project scales into the exported representation, and quantize weights.
5. Export through the existing compressed-tensors/ModelOpt path and load its supported engine kernel.

The pinned llm-compressor NVFP4 example provides the sequential AutoRound workflow. ModelOpt's documented scale-learning scope is per-block NVFP4 weight scales, not global or activation scales. One learned scale per 16 weights reduces optimizer state compared with joint weight learning; it does not reduce all forward/backward computation sixteenfold.

## Impact and Results

The pinned AutoRound example reports Llama-3.1-8B-Instruct NVFP4 GSM8K results with vLLM/lm_eval, five-shot prompts, `add_bos_token=true`, and automatic batching. Moving from 0 to 200 AutoRound iterations raises strict-match exact accuracy from 0.6702 to 0.6945, while flexible-extract accuracy falls from 0.7362 to 0.7210. Strict-match and flexible-extract apply different answer-extraction rules to the same task responses, so these values reflect both answer content and output format.

Recovery changes offline rounding/scales rather than adding a runtime optimizer. Its cost is calibration/optimization time and memory; joint training adds weight/optimizer state. An unchanged exported format can retain the same kernel execution while altering quality. Unsupported kernels, broken export, or unobserved experts are not repaired by lower reconstruction loss.

## References

- [llm-compressor AutoRound NVFP4 example](https://github.com/vllm-project/llm-compressor/tree/990fdd2fb41af48f105dbdebd088a96b627701b1/examples/autoround/quantization_w4a4_fp4).
- [Model Optimizer scale-learning method and current scope](https://nvidia.github.io/Model-Optimizer/announcements/scale-learning.html).
- [vLLM deployment formats](https://docs.vllm.ai/en/latest/features/quantization/modelopt/).

See also [MoE execution](../moe-execution/).
