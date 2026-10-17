<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce MoE materialization through gather-fused grouped GEMM

English | [简体中文](moe-gather-fusion_zh.md)

## Observations and Motivation

Sparse MoE can spend more time gathering activations, writing intermediates, and reducing outputs than its reduced arithmetic count suggests. This is particularly visible with narrow expert intermediates or few rows per expert. SonicMoE fuses input gathering and activation into grouped GEMM while retaining a separate output reduction where that better overlaps I/O.

## Principles and Analysis

For input `X[T,H]`, each token chooses `K` experts. Expert `e` receives `m_e` rows, with `sum(m_e)=T*K` for valid routes:

```text
H_e = gather(X, token_indices_e) @ W1_e
A_e = SwiGLU(H_e)
Y_e = A_e @ W2_e
y_t = sum_{e selected by t} routing_weight[t,e] * Y_e[row_e(t)]
```

A separate gathered input would write and reread routed activations in HBM. Loading indexed rows directly into shared memory removes that materialization. Fusing SwiGLU into FC1's epilogue removes the need to expose its pre-activation tensor as a separate stage.

Tile waste remains: expert rows `[1,17,129]` with row tile 128 use 147 valid rows across 512 rows of tile capacity. This geometry illustrates underfill, not a measured slowdown; masks, arithmetic, and weight reuse have different costs.

## Design

Build expert row indices without changing the router's selected experts or weights. Grouped FC1 consumes the indices directly and produces activated intermediates; grouped FC2 writes contiguous expert-major outputs. A gather-and-sum kernel restores original token order and applies routing weights.

On Hopper, scattered global stores can impede overlap between epilogue I/O and the next MMA tile. Contiguous asynchronous output plus a separate reduction can therefore beat more aggressive scatter fusion. The objective is less expensive traffic on the critical path, not simply fewer kernels.

## Implementation

1. Count valid routes, form expert offsets, and record original token indices and each route's expert-local row.
2. In FC1, gather each input tile into shared memory and multiply by the matching expert's gate/up weights; apply SwiGLU in the epilogue.
3. Run FC2 on activated rows and store results contiguously by expert.
4. Gather the selected expert results for each token, multiply by original routing weights, and sum into the output.

The pinned `sonicmoe/functional/forward.py` uses CuTeDSL, QuACK, and Triton for Hopper/Blackwell. Convert concatenated checkpoint gate/up weights to the required interleaved layout without swapping their roles. BF16 operands and router normalization remain part of the execution contract. EP dispatch that already supplies expert-major rows can eliminate the need for another local gather.

## Impact and Results

Gather fusion saves a routed-input HBM round trip, and activation fusion removes a stage boundary and pre-activation materialization. Contiguous FC2 output still incurs a reduction read but supports efficient asynchronous stores. SonicMoE's paper primarily measures training, including backward activation storage; those memory savings do not quantify decode savings. Its optional token-rounding router changes expert selection and requires training co-design, unlike the routing-preserving execution described here. Underfilled tiles and final aggregation remain costs even after gathering is fused.

## References

[SonicMoE paper](https://arxiv.org/abs/2512.14080), algorithms 1–4, sections 3–5 and appendices B/F; [repository](https://github.com/Dao-AILab/sonic-moe/tree/7396f3e604827d8186c2e16e64b28ee33d3defd0), including `sonicmoe/functional/forward.py` and `benchmarks/moe-cute.py`.
