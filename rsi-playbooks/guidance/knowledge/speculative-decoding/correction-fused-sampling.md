<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce correction cost with fused rejection sampling

English | [简体中文](correction-fused-sampling_zh.md)

## Observations and Motivation

After target verification, vocabulary projection, filtering, residual sampling, RNG, packing, and requested logprobs still consume time and memory. Fusing these stages can reduce iteration cost without changing the proposer or training another model.

## Principles and Analysis

Let p be the desired target distribution at a candidate history and q the distribution that actually generated x. Accept x with min(1,p(x)/q(x)); on rejection sample from normalized max(p-q,0). If all drafts succeed, sample a target bonus. This preserves the target distribution, not same-seed text equality.

For p=(0.6,0.4) and q=(0.8,0.2) over {A,B}, A is accepted with probability 0.75 and rejection produces B. Thus P(A)=0.8×0.75=0.6. An unconditional target draw on rejection would overproduce A. A deterministic proposal has point-mass q, not its pre-argmax softmax.

## Design

Keep probability processing, acceptance, correction, and output packing on the device. Apply temperature, top-k/top-p, grammar constraints, and penalties to p at each hypothetical causal history; reranking must be reflected in q. Eager residual computation can do unused work while avoiding a host synchronization. Exact block verification is a different probability coupling that can retain longer prefixes; it is not token-wise rejection with mismatches skipped.

## Implementation

The conceptual token-wise algorithm is:

```text
for j in candidate order:
    p = processed_target_distribution(history + candidates[:j])
    q = realized_proposal_distribution(j)
    if uniform() <= min(1, p[x_j] / q[x_j]): append(x_j)
    else: append(sample(normalize(max(p-q, 0)))); stop
if every candidate was accepted: append(sample(target_bonus_distribution))
commit emitted prefix; roll back uncommitted state
```

This describes the probability rule, not an engine API. The pinned vLLM sampler computes recovered candidates before choosing the emitted prefix and separates greedy argmax from random sampling. FlashInfer's fused chain API takes draft probabilities [B,K,V], IDs [B,K], and target probabilities [B,K+1,V]. Its independent accepted count can include positions after rejection; emitted-draft count is the actual prefix and excludes the bonus.

## Impact and Results

Fusion removes intermediate probability traffic, repeated vocabulary passes, and launch overhead; dense probabilities still require O(BKV) storage if the API materializes them. Top-k/top-p sorting and requested logprobs can remain major costs. The pinned SGLang guide supports exact block verification on CUDA/ROCm for stochastic EAGLE/EAGLE3/NEXTN with topk=1 and full-vocabulary proposals, plus supported sampled DFlash/DSpark paths. It adds vocabulary reductions; greedy point-mass DFlash gains no longer prefix. Lowering approximate acceptance thresholds is not this exact optimization. After rejection, later logits describe the wrong history and cannot be emitted; [recurrent replay](../attention-state/state-write-replay.md) handles non-KV state recovery.

## References

- [Pinned vLLM sampler](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/sample/rejection_sampler.py): processing, point-mass proposals, residual selection, and packing.
- [FlashInfer chain API](https://docs.flashinfer.ai/generated/flashinfer.sampling.chain_speculative_sampling.html): shapes, RNG offsets, and accepted/emitted counters. Match this rolling reference to the installed library version.
- [Pinned SGLang guide](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx): supported block-verification combinations and vocabulary-reduction cost.
- [Block Verification Accelerates Speculative Decoding](https://arxiv.org/abs/2403.10444) defines the original algorithm; the pinned engine guide above defines the serving boundary.
