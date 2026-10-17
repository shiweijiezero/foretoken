<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Match MLA decode kernels to quantized cache layouts

English | [简体中文](mla-layout-matching_zh.md)

## Observations and Motivation

Multi-head Latent Attention (MLA) stores latent history shared by query heads, but decode still scans it and combines partial outputs. Scale loads, scattered gathers, dequantization, and projection can dominate after cache compression. The cache producer and attention consumer must agree on a model-specific physical representation; a smaller format interpreted incorrectly is not an optimization.

## Principles and Analysis

With latent state `c_t` and positional key `r_t`, projection absorption gives a simplified head computation:

```text
score(t) = q_latent · c_t + q_position · r_t
z        = sum_t softmax(score(t)) * c_t
output   = project(z)
```

The cache is not an expanded K/V pair for every head. Absorption changes kernel dimensions while preserving the model's projection and positional rules. Sparse selection changes which tokens contribute; quantization changes their represented values.

Split attention produces normalized partial outputs `o_i` and log-normalizers `L_i`. Merge with `L=logsumexp_i(L_i)` and `o=sum_i exp(L_i-L)*o_i`, not an unweighted average.

## Design

Select the layout together with the model and hardware. The earlier FlashMLA V3.2 sparse format uses 512 E4M3 latent bytes, four FP32 scales, and 64 BF16 RoPE values: 656 bytes per token. Each scale covers 128 latent values.

V4.1 instead stores 512 FP8 values including 64 RoPE dimensions, plus sixteen E8M0 scales: 528 bytes. Its optional FP4 `extra_k_cache` packs 512 E2M1 values into 256 bytes with 32 E4M3 scales, totaling 288 bytes, alongside the FP8 main cache. V4.0 has separate dimensions and page-scale placement. These are distinct formats, not interchangeable compression levels.

## Implementation

1. Pack each cache row and its scales using the selected model's layout, preserving positional components and page addressing.
2. Prepare selected physical indices and `get_mla_metadata` for the current lengths, top-k, query count, and head configuration.
3. In `flash_mla_with_kvcache`, gather rows, decode their scales, compute latent/positional scores, and accumulate latent-weighted partial outputs.
4. Merge splits with their log-normalizers, then apply the model's output projection. Reordered gathers retain the association between indices, values, and positional features.

The September 30, 2026 breaking change directs V3/V3.2/V4.0 and Hopper users to the cited earlier revision. That sparse path covers SM90/SM100; current V4.1 documents SM100/SM103 and Ascend 950 with quantized-only decode and different software requirements. When selection dominates, see [indexer head routing](indexer-head-routing.md).

## Impact and Results

The documented 656-, 528-, and 288-byte rows quantify storage, not speedup: they belong to different model layouts, and the 288-byte representation is an extra cache rather than a main-cache substitute. Quantization reduces value traffic but adds scale reads and decoding; sparse gathers can remain bandwidth-inefficient when indices span distant pages. Long-context decode benefits when cache traffic is dominant, whereas small batches can remain launch-bound. Metadata, split reduction, cache insertion, and projection still contribute to full-model latency.

## References

- [FlashMLA current README](https://github.com/deepseek-ai/FlashMLA/blob/2e5429fc5653bab6e081f09477126f731882a6a9/README.md): V4.1 formats, hardware scope, API, and September 30 breaking change.
- [Explicit earlier revision `ba89a3466e9470ad08ab39738d4e7bb66989e1e7`](https://github.com/deepseek-ai/FlashMLA/blob/ba89a3466e9470ad08ab39738d4e7bb66989e1e7/README.md): V3.2/V4.0 layouts and earlier dense/sparse decode contracts.
- [DeepSeek-V3.2 report, v1](https://arxiv.org/html/2512.02556v1): DSA's use of MLA MQA and model-level sparse selection.
