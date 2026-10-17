<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Hide attention softmax cost through asynchronous pipelines

English | [简体中文](softmax-pipeline-overlap_zh.md)

## Observations and Motivation

Long-context prefill can spend substantial time on exponentials, row reductions, probability conversion, and output rescaling even when its matrix products are fast. Faster Tensor Cores expose this imbalance: increasing a GEMM tile alone can raise register pressure without shortening the softmax dependency chain. FlashAttention-4 organizes these stages into an asynchronous Blackwell pipeline.

## Principles and Analysis

Dense attention can stream key/value tiles while maintaining a row maximum `m`, denominator `l`, and unnormalized output `O`. For scaled, masked scores `S`, the exact online update in real arithmetic is:

```text
m' = max(m, rowmax(S))
a  = exp(m - m')
P  = exp(S - m')
l' = a*l + rowsum(P)
O' = a*O + P*V
```

Both accumulated quantities must use the same reference maximum. Otherwise a later large logit leaves earlier contributions overweighted. Streaming avoids quadratic score storage; overlap, rather than fewer attended tokens, addresses the remaining execution cost.

## Design

FlashAttention-4 interleaves two query tiles. Tensor Cores compute one while softmax warpgroups process the other; a separate correction warpgroup rescales the accumulated output. Asynchronous MMA places intermediates in Tensor Memory. Larger tiles amortize row statistics and scheduling, at the cost of more live resources.

The method also distributes exponentials between special-function units and polynomial/FMA evaluation. Conditional rescaling allows the reference maximum to lag small increases, reducing vector corrections while keeping probabilities, denominator, and output consistently normalized. Approximation and activation rounding still affect numerical precision.

## Implementation

1. Initialize `m=-infinity`, `l=0`, `O=0` for each query tile; stage Q and the first K/V tile.
2. Issue QK MMA asynchronously and signal score readiness. While softmax consumes that tile, stage or compute independent work for the other query tile.
3. Produce probabilities and normalization statistics, issue PV MMA, and apply the corresponding output correction before reusing its storage.
4. Repeat over valid K/V tiles and write `O/l` after the final correction.

The pinned CuTe interface dispatches to `FlashAttentionForwardSm100` with shape-specific tiles, split counts, and one- or two-CTA schedules. Its SM100/SM110 forward accepts FP16/BF16 and selected FP8 formats with head-dimension restrictions; FP8 output is BF16 and descales have a separate shape contract. Paged storage, GQA, causal masks, and short queries affect dispatch. Hopper and non-CUDA devices require their own kernels.

## Impact and Results

The pipeline reduces exposed softmax and output-correction time while preserving dense token coverage. Software exponentials are useful when FMA resources are available; excessive tile size or insufficient independent queries can erase overlap through occupancy loss or pipeline starvation. Split-KV reduction and engine-side metadata preparation remain outside this saving. Faster long-prefill kernels can also concentrate contention with concurrent decode, so the mechanism is a kernel critical-path improvement rather than a guaranteed service-wide latency reduction.

## References

- [FlashAttention-4, March 2026, v1](https://arxiv.org/html/2603.05451v1): forward pipeline, software exponentials, conditional rescaling, and reported comparisons.
- [CuTe interface at `1fc0221183e799363001394a12dd9e78e15f5dda`](https://github.com/Dao-AILab/flash-attention/blob/1fc0221183e799363001394a12dd9e78e15f5dda/flash_attn/cute/interface.py): actual architecture, dtype, shape, and schedule dispatch.
- [SM100 forward implementation at the same revision](https://github.com/Dao-AILab/flash-attention/blob/1fc0221183e799363001394a12dd9e78e15f5dda/flash_attn/cute/flash_fwd_sm100.py): asynchronous pipeline and correction-stage ownership.
