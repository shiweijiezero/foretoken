<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Remove serial draft forwards with parallel blocks

English | [简体中文](parallel-block-drafting_zh.md)

## Observations and Motivation

An autoregressive draft proposing K tokens can require K sequential auxiliary forwards, each paying launch, attention, and output-projection overhead. A fast target does not remove that serial drafting cost. Parallel block prediction replaces the chain of forwards with one block forward.

## Principles and Analysis

PARD adapts a small autoregressive model to future-position mask prediction using token context, without target hidden features. DFlash is a separately trained feature-conditioned block-diffusion drafter. A verified anchor starts its masked block, whose positions interact bidirectionally and are predicted in one pass. The target remains causal. Independent slot plausibility does not guarantee a coherent block.

At comparable committed length, the cost changes from K×draft_forward + verify(K) + correction to block_forward(K) + feature_handling + verify(K) + correction. Block width still increases verification rows, projections, and buffers.

## Design

Use one target plus one auxiliary model. DFlash projects and normalizes concatenated target-layer features into extra draft keys/values; PARD constructs token-level masked inputs instead. Verify the proposed linear continuation with a causal target pass, then commit the accepted prefix and correction/bonus. Extend draft context only with committed tokens/features. Truncating verification after a one-pass draft saves target work, not drafting already completed; see [adaptive budgets](adaptive-verify-budget.md).

## Implementation

The core DFlash flow is:

1. Freeze the target, generate training responses, choose clean anchors, and mask future slots.
2. Train one-pass block prediction with block-local bidirectional attention and separated training blocks; weight early positions because an early error invalidates later proposals.
3. Load compatible target-specific weights and obtain features after prefill/commit.
4. Predict the masked block once, verify causally, and discard the uncommitted suffix.

PARD uses future-mask objectives and conditional drop-token adaptation. vLLM documents `amd/PARD-Qwen3-0.6B` with `parallel_drafting: true`; DFlash publishes target-specific weights. Merged DFlash2 integrations require their matching checkpoints. Pinned SGLang infers block size when possible, requires a draft window at least that large, excludes pipeline parallelism and DP attention, and disables overlap scheduling and mixed chunked prefill. Block conventions may include the anchor. Stochastic verification needs the actual proposal distribution.

## Impact and Results

The DFlash paper reports 5.15× over autoregressive decoding for Qwen3-8B on GSM8K with an NVIDIA H200 and Transformers backend: thinking disabled, temperature 0, maximum 2048 output tokens, a five-layer drafter, and block size 16. Table 1 reports average acceptance length τ=6.54 tokens, including the target bonus token.
Parallel drafting removes serial auxiliary launch dependencies but adds feature handling and wider block work. Early rejection wastes target rows; larger blocks need not increase committed output. Scheduler exclusions are additional deployment costs. [Joint selection](draft-joint-selection.md) addresses coherence separately.

## References

- [PARD v4](https://arxiv.org/html/2504.18583v4): parallel masks, target independence, and adaptation objectives; [pinned vLLM example](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/parallel_draft_model.md).
- [DFlash paper](https://arxiv.org/html/2602.06036): feature-conditioned one-pass drafting and training; [official repository](https://github.com/z-lab/dflash) at `07ebd93db9f472af339b644bb70221ad8428328a` lists public model collections.
- [vLLM #52816](https://github.com/vllm-project/vllm/pull/52816) merged 2026-08-21 as `b389ac29465b33f9e9c534df221ea3c129e9793f`; [SGLang #35371](https://github.com/sgl-project/sglang/pull/35371) merged 2026-08-19 as `c14312a66420b75ca9a11bf1817c4db1fa26b097`.
- [Pinned SGLang guide](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx) and [SpecForge training guide](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md) establish current inference/training boundaries.
