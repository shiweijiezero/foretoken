<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce sampling work through fused radix selection

English | [简体中文](sampling-radix-selection_zh.md)

## Observations and Motivation

After model-forward acceleration, vocabulary filtering and sampling can dominate post-forward work. Speculative verification increases the number of rows processed. Full-vocabulary sorting and intermediate tensors can cost more than selecting the small support actually needed for sampling.

## Principles and Analysis

Top-k requires the selected support, not a complete ordering of all V vocabulary entries. Top-p then finds a cumulative-mass boundary inside the chosen distribution. Performing these stages on a compact candidate set reduces repeated full-row processing.

Filter order changes semantics. For `[0.5, 0.3, 0.2]`, `k=2`, and `p=0.6`, top-k-first retains mass 0.8; the first item alone exceeds `0.6×0.8=0.48`. Top-p-first needs two items to exceed 0.6 in the original distribution. Fusion must preserve the intended order, tie policy, and random-number assignment.

## Design

FlashInfer CAKE uses cluster-wide radix selection to form a compact slab, followed by top-p and inverse-CDF sampling. Distributed shared memory combines selection information across blocks. Small-k cases can fuse the final work into the selection kernel; larger cases use a separate stage, optionally with dependent launch.

The specialized path uses contiguous float32 probability rows on supported Hopper-or-newer NVIDIA devices. Unsupported inputs route to the existing deterministic sampler. Greedy decoding is a separate argmax path and can avoid filtering work when other consumers do not need probabilities.

## Implementation

For each probability row:

```text
select exact top-k by radix passes
write selected (probability, token_id) pairs to a compact slab
sort the slab by descending probability, then ascending token_id
find the shortest prefix meeting top_p × top-k mass
normalize its mass and sample with the Philox draw
```

The API supports a slab of up to 1,024 entries. It specifies deterministic boundary ties and fixed-point cumulative sums. A routing query reports whether the specialized implementation or fallback will run. Penalties, constraints, logits-to-probability conversion, and any required copies occur around this core and remain part of total sampling cost.

## Impact and Results

The specialized path limits the final ordering and cumulative-mass work to at most 1,024 candidates instead of the full vocabulary. Supported cases with `top_k_max ≤ 64` can use one fused kernel rather than the two-stage path described by the API.

This removes launches and full-vocabulary intermediate traffic. Conversion or extra float32 copies can offset the reduction, especially for small batches. For [speculative decoding](../speculative-decoding/), the relevant benefit is lower sampling cost per committed token, not simply per proposed row.

## References

- [CAKE sampling documentation](https://docs.flashinfer.ai/api/cake_sampling.html): supported inputs, route query, and fused cases.
- [FlashInfer implementation](https://github.com/flashinfer-ai/flashinfer/blob/d6dd9f336dcd9e4820b1583d20077804fe61147c/flashinfer/cake_sampling.py): radix selection, tie policy, RNG, and fallback.
- [FlashInfer sampling API](https://docs.flashinfer.ai/api/sampling.html): other filtering and sampling paths.
