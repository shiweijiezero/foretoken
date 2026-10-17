<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Parallelize KDA prefill with chunked recurrence

English | [简体中文](kda-prefill-chunking_zh.md)

## Observations and Motivation

A recurrent attention layer updates one matrix per sequence. Sequentially applying those dependent updates to a long prompt underuses parallel hardware, even though decode naturally processes one token at a time. Kimi Delta Attention (KDA) uses channel-wise forgetting and a chunk formulation that converts much of prefill into matrix products.

## Principles and Analysis

Use key-first state `S[K,V]`, column vectors `q,k` of length `K`, `v,r,o` of length `V`, channel decay `alpha[K]`, and scalar update strength `beta`. The key is normalized and `q` includes output scaling:

```text
A  = diag(alpha)
Sd = A*S
r  = v - transpose(Sd)*k
S' = Sd + beta*k*transpose(r)
o  = transpose(S')*q
```

Thus `S'=(I-beta*k*k^T)*A*S+beta*k*v^T`. Subtracting the predicted value prevents repeated keys from accumulating as simple additive memory. With `alpha=[0.99,0.10]`, one row is mostly retained while the other is strongly forgotten; scalar-decay GDN cannot express that within a head. Decay must precede residual calculation: the diagonal matrix generally does not commute with `k*k^T`.

## Design

Partition prefill into chunks. Cumulative diagonal decays and low-rank corrections encode the causal dependencies inside each chunk; a triangular transformation produces auxiliary matrices for parallel matrix products. Carry only the boundary state sequentially between chunks. Decode instead fuses the one-token update and readout.

KDA requires trained KDA weights. Kimi Linear places one global MLA layer after every three KDA layers, so total model storage still grows with MLA history. The chunk transformation preserves recurrence, unlike replacing this hybrid architecture with generic linear attention.

## Implementation

1. Generate and normalize Q/K, values, channel gates, and beta using the model's own projection and gate rules.
2. Pack sequences with `cu_seqlens`; compute cumulative log-decays within each chunk and form the causal triangular correction system.
3. Solve the transformed intra-chunk dependencies, combine them with the incoming state, and compute token outputs through parallel products.
4. Produce the chunk's final state for the next chunk; hand the last state to fused-recurrent decode with the same orientation and scaling.

The pinned FLA chunk API supports sizes 32 or 64 and key dimensions up to 256. Supplied initial state is FP32; activations commonly use BF16. Log-space gates, raw gates transformed in-kernel, and post-sigmoid beta are distinct inputs. Default state layout is `[N,HV,K,V]`, with a value-first option. The public recurrent wrapper does not expose all lower-level state-indexing or accepted-token arguments; batching slots remain an engine responsibility.

## Impact and Results

Chunking replaces a long token-by-token dependency chain with Tensor-Core-friendly intra-chunk work and sequential boundary handoff. It adds auxiliary matrices and temporary storage, while fixed recurrent state avoids per-token K/V growth within KDA layers. Decode still reads and writes that matrix for each active request. Ragged packing avoids treating unrelated sequences as one recurrence. CUDA and Ascend backends have separate implementations; their presence does not imply identical schedules. [Replay buffers](state-write-replay.md) address state writes separately, and scalar-decay replay formulas do not directly implement KDA's channel gates.

## References

- [Kimi Linear, 2025-10-30, v1](https://arxiv.org/html/2510.26692v1): KDA recurrence, diagonal-plus-low-rank chunk execution, and hybrid architecture.
- [FLA chunk API at `1fe52b5d05c53601885610fab3d39157647d836d`](https://github.com/fla-org/flash-linear-attention/blob/1fe52b5d05c53601885610fab3d39157647d836d/fla/ops/kda/chunk.py): gate contracts, state precision, packing, and chunk restrictions.
- [FLA fused-recurrent implementation at the same revision](https://github.com/fla-org/flash-linear-attention/blob/1fe52b5d05c53601885610fab3d39157647d836d/fla/ops/kda/fused_recurrent.py): update order, state orientation, and public/lower-level API distinction.
