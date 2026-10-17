<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce intermediate traffic through layout-aware operator fusion

English | [简体中文](intermediate-traffic-fusion_zh.md)

## Observations and Motivation

Normalization, activation, and layout conversions can be individually short but expensive as a sequence of launches, allocations, and full-tensor passes. The bottleneck is often the intermediate representation crossing operator boundaries rather than arithmetic inside one operator.

## Principles and Analysis

Removing a separate write and subsequent read of an N-element tensor with s bytes per element avoids approximately `2Ns` bytes of logical traffic. Cache residency determines the actual HBM reduction.

Fusion must also fit the producer's lane layout and the consumer's access pattern. Matrix accumulators are arranged for matrix instructions; CUTLASS epilogues exchange and convert values before coalesced stores. More fused work extends live ranges and can increase registers, shared memory, and synchronization.

## Design

vLLM v0.31.0 fuses per-head Q/K RMSNorm with RoPE on packed QKV. V is unchanged. A graph-pattern pass selects the fused operation only for supported dtype, head geometry, normalization epsilon, and rotary layout.

The replacement uses `auto_functionalized`: the native operation's mutation appears as an updated returned tensor in the graph. Other consumers and alias relationships determine whether physical storage can be reused.

## Implementation

The matched computation is:

```text
split packed QKV into Q, K, V views
reshape Q and K into heads
for each Q/K head:
    normalized = x * reciprocal_sqrt(mean(x*x) + epsilon) * weight
    rotate normalized values using positions and the RoPE convention
return transformed Q, transformed K, unchanged V
```

The compiler replaces this sequence with the fused packed-QKV call, then splits its returned tensor. The cited pass supports FP16/BF16, head dimensions 64/128/256, epsilon `1e-5` or `1e-6`, and NeoX or interleaved rotary layouts. It normalizes view/reshape graph representations so equivalent sequences can match.

A direct-output design can further avoid a layout conversion when the next consumer accepts that layout. Every consumer needing the original value must retain access to it.

## Impact and Results

Fusion removes intermediate tensor traffic and launch boundaries while values remain available inside the fused execution. It is most useful when those costs dominate the surrounding layer.

Extra register pressure or a less efficient GEMM can outweigh the saving. Decode and prefill have different shape and launch-cost ratios, so their useful fusion boundaries can differ. Existing compiler fusion also determines whether a proposed replacement removes any additional work.

## References

- [vLLM QK norm/RoPE fusion](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion/qk_norm_rope_fusion.py): pattern, supported geometry, and replacement.
- [vLLM fusion passes](https://github.com/vllm-project/vllm/tree/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion).
- [CUTLASS epilogue and memory organization](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md).
- [PyTorch custom operator integration](https://docs.pytorch.org/tutorials/advanced/cpp_custom_ops.html): metadata, mutation, and alias contracts.
