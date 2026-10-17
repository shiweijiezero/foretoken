<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce indexer cost with query-dependent head routing

English | [简体中文](indexer-head-routing_zh.md)

## Observations and Motivation

DeepSeek Sparse Attention (DSA) first scans index keys, then lets expensive MLA attend to selected tokens. A fixed final top-k bounds the MLA work but exposes linear indexer cost in decode and quadratic work in full prefill. MISA reduces scoring along the head axis before selecting tokens, using pretrained DSA models rather than changing the main attention kernel.

## Principles and Analysis

For query `t`, historical token `s`, indexer head `j`, shared index key `k(s)`, and learned weight `w(t,j)`, DSA scores:

```text
I(t,s) = sum_j w(t,j) * ReLU(q(t,j) · k(s))
selected(t) = topk_s I(t,s)
```

The indexer is trained against aggregated attention before sparse model training; the DeepSeek-V3.2 report selects 2,048 tokens per query. This score is not MLA softmax.

MISA mean-pools keys in blocks of `B`, estimates each head's affinity, and scans history using only `h` selected heads out of `H`. For history length `L`, approximate scoring work falls from `H*L` to `H*ceil(L/B)+h*L`. All historical index keys are still read, so arithmetic savings need not imply comparable byte savings.

## Design

Maintain pooled keys alongside index-cache insertion and route heads separately for each query. Prefill pooling respects each query's causal boundary; decode uses the growing valid history.

The two-stage MISA† variant makes a larger candidate set with routed heads, then reranks only those candidates with all heads. Its example uses 8 routed heads, pooling blocks of 1,024 tokens, 8,192 candidates, and 2,048 final tokens. Reranking improves scores inside the candidate set but cannot recover an excluded token; mean pooling can hide an isolated high-affinity key.

## Implementation

1. Pool valid index keys and compute coarse affinities for all heads.
2. Select the query's routed heads and evaluate the weighted ReLU score across valid historical keys using those heads.
3. Take token top-k directly, or take the larger candidate set and recompute the full-head DSA score on it before final top-k.
4. Pass selected physical locations to unchanged sparse MLA; keep index-key scales and attention-cache formats separate.

The author's one- and two-stage patch packages target vLLM 0.17.0, Transformers 5.2.0, and TileLang 0.1.7.post3; install only one patch. `misa_ops.py` and `indexers.py` own routing and reranking. Query-norm routing is a different selector. The cited upstream vLLM indexer already provides quantization, candidate-block masking, and distributed exact top-k merging; exact merging preserves a given score order, unlike approximate head pruning.

## Impact and Results

MISA targets DeepSeek-V3.2 and GLM-5 without retraining. It removes many head-key dot products at long contexts, but adds pooled-key maintenance, routing, and possibly reranking. Short contexts can fail to amortize those stages, and top-k or key traffic can become dominant after scoring shrinks. The final token set can differ from full-head DSA, especially for rare keys hidden by pooling; that approximation is distinct from FP8/FP4 scoring error and attention-cache quantization. The reported configuration describes selection work, not a universal service speedup.

## References

- [DeepSeek-V3.2 report, v1](https://arxiv.org/html/2512.02556v1): indexer formula, training, top-k, and MLA relationship.
- [MISA, 2026-05-08, v1](https://arxiv.org/html/2605.07363v1): pooled-key head routing, reranking, complexity, and empirical fidelity.
- [Author implementation README at `dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc`](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/README.md), [two-stage kernels](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/misa_vllm_patch_2stage/misa_vllm_patch/misa_ops.py), and [indexer wrapper](https://github.com/MuLabPKU/TransArch/blob/dc9f3a3b7938bd26e165b0c99e3b6ea627d1ecfc/MISA_preprint/misa_vllm_patch_2stage/misa_vllm_patch/indexers.py): released patch scope and actual routing/reranking.
- [vLLM indexer at `6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/sparse_attn_indexer.py): scoring/quantization/top-k and candidate-block execution at this revision.
