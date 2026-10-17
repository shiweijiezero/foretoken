<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce KV Reads Through Query-Aware Page Selection

English | [简体中文](kv-query-page-selection_zh.md)

## Observations and Motivation

Reducing KV reads is different from reducing stored KV. A selector can retain all information and choose different pages for each query; an eviction method permanently removes entries from the active cache. The distinction determines whether a later question can recover information missed by an earlier one.

## Principles and Analysis

[Quest, June 2024](https://arxiv.org/html/2406.10774v1) keeps the complete cache and adds minimum and maximum key values per dimension for each page. Given query `q`, page `p` receives the score bound

`U_p(q) = sum_i max(q_i min_p(k_i), q_i max_p(k_i))`.

For any key in that page, its dot product with q cannot exceed this coordinate-wise bound. The extreme values may belong to different tokens, so the bound can be loose. Ranking bounds is not an exact top-token search and does not certify that omitted probability mass is small.

If page size is `S`, context length `N`, and selected-page count `K`, approximate metadata-plus-KV traffic relative to a full K/V scan is `1/S + KS/N`, assuming equal element widths and ignoring selection/workspace traffic. For example, with `S=16`, `N=32768` and `K=128`, this is about 12.5%, not a prediction of 8× service speedup. Scoring, top-k, launch costs and non-attention layers remain.

## Design

Keep full paged K/V and a compact min/max summary for each key page. Update summaries as tokens arrive. Each decode query selects pages afresh, then the attention kernel reads their complete K/V. Selection belongs with attention execution; cache storage supplies page geometry and, if pages are offloaded, their retrieval.

The reported Quest configuration leaves the first two layers dense because useful sparsity is weaker there. A page budget controls read traffic, not retained-cache capacity. Multi-fact aggregation can require more pages than a single passkey, and moving unselected pages out of HBM adds a separate fetch dependency.

## Implementation

For each sparse layer and attention head:

1. Append the new key/value and update the containing page's coordinate-wise minima/maxima.
2. For query `q`, compute each page's bound using maxima where `q_i >= 0` and minima where `q_i < 0`.
3. Select the budgeted top-k page IDs, gather their valid token positions, and read only those K/V ranges.
4. Compute query-key scores, apply the original causal mask, normalize over the selected tokens and accumulate values.

The result is approximate attention over a query-dependent subset, not certified exact attention. The original Quest kernels implement scoring and selection; vLLM's paged geometry, LMCache's sparse segment retrieval, Dynamo KVBM and Mooncake transfers provide cache infrastructure rather than this scoring algorithm.

## Impact and Results

At 32K context and a 2,048-token budget, Quest reports 7.03× self-attention acceleration over full-attention FlashInfer for Llama2-7B on an RTX 4090 with CUDA 12.2. Its end-to-end evaluation on an Ada 6000 at the same context/budget reports 1.74× speedup with FP16 weights and 2.23× with 4-bit weights over FlashInfer. Kernel and end-to-end results use different GPUs and should remain separate comparisons.

The gain comes from avoiding most full-cache reads. Metadata scoring, top-k and non-attention layers remain; all K/V is retained, so this method does not inherently reduce HBM capacity. Because selection is repeated for each query, an earlier unselected page remains available for a later question.

## References

- [Quest, June 2024](https://arxiv.org/html/2406.10774v1)
- [NIXL compatibility document](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)
- [MP blend retrieval](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [KVBM onboarding](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/docs/onboarding.md)
- [Transfer Engine](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/mooncake-transfer-engine/include/transfer_engine.h)
