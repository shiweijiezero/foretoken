<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce MetaX INT8 Traffic Through Compatible Stage Fusion

English | [简体中文](metax-int8-fusion_zh.md)

## Observations and Motivation

On MetaX MACA, dense INT8 execution can include input-layout copies, quantization, scaled GEMM and output processing. Routed MoE adds expert layouts, activation, requantization and combine. Repeated materialization between compatible stages increases traffic; a CUDA-like interface does not imply identical native layouts or workspace semantics.

## Principles and Analysis

For symmetric per-token input and channelwise weight scales:

```text
Y[m,n] ≈ sx[m] sw[n] Σk qx[m,k] qw[n,k] + bias[n]
```

Dynamic input quantization first reduces over K for each row. Separate normalization and quantization can write normalized BF16 values and read them again before writing INT8/scales. A compatible fused producer avoids that intermediate traffic, but must preserve both reductions and the consumer's layout.

If normalization spans tensor shards, its sum of squares needs cross-shard reduction. Fusing local work cannot omit it. MoE's routed rows and per-expert padding change scale placement, so a dense [M,K] fusion does not directly define an expert layout.

## Design

Keep scaled GEMM with the plugin's selected kernel; fuse a compatible producer boundary such as normalization plus quantization rather than replacing native dispatch. vLLM-metax's `MctlassScaledMMLinearKernel` obtains weights/scales, makes input contiguous, quantizes and calls its scaled-MM wrapper. Static scales come from the layer; asymmetric inputs require the zero-point-aware path.

Its compressed-tensors INT8 MoE requires channelwise weights and dynamic per-token inputs. The modular expert kernel receives routing/shared-expert data, and the oracle selects supported Triton, batched Triton or batched DeepGEMM paths. The selected path owns the expert layout.

## Implementation

A symmetric RMSNorm-plus-quantization producer computes:

```text
for each token row x:
    sumsq = sum(x[k]**2)
    if normalized dimension is sharded: all-reduce sumsq
    u[k] = x[k] * rsqrt(sumsq/global_K + epsilon) * gamma[k]
    u = cast to the owning normalization's output dtype
    amax = max(abs(u[k])) over the consumer's input dimension
    if one scale spans K shards: all-reduce amax with MAX
    sx = amax / 127
    qx[k] = 0 if sx == 0 else clamp(round(u[k]/sx), -127, 127)
    write qx and sx in the scaled-GEMM consumer layout
scaled GEMM accumulates qx × qw, applies sx × sw and bias
```

This is a fusion algorithm for the symmetric case, not a claim that the plugin already fuses RMSNorm. Match the owning quantizer's rounding and scale conventions. For MoE, route rows with their scales, execute gate/up GEMM, SiLU-and-multiply, requantize that activation for down GEMM, then apply routing weights and combine via the selected modular path. Keep valid-row masks, inverse routing and stream/workspace lifetimes intact. Use a matching released vLLM/plugin/MACA SDK/operator-library combination and prepared INT8 weights with the required scale granularity.

## Impact and Results

Compatible producer fusion removes a normalized intermediate write/read and possibly a launch, while retaining the necessary row reductions and INT8 output. Avoiding redundant contiguous copies reduces traffic only when native consumers accept the supplied strides/layout. Expert routing, requantization and padding can still dominate, and fusion may lose parallelism.
## References

- [MetaX dense INT8 registration and execution](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/registry/linear_kernels/int8.py).
- [Current compressed-tensors INT8 MoE method](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/registry/quant_config/compressed_tensors_moe/compressed_tensors_moe_w8a8_int8.py).
- [Current INT8 MoE oracle](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/model_executor/layers/fused_moe/oracle/int8.py) and [release compatibility guidance](https://github.com/MetaX-MACA/vLLM-metax).

 · [Quantization](../quantization/) · [Knowledge index](../README.md)
