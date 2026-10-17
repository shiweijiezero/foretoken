<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Improve draft coherence through joint candidate selection

English | [简体中文](draft-joint-selection_zh.md)

## Observations and Motivation

A parallel drafter can predict locally plausible tokens that disagree jointly, such as introducing one identifier and later using another. Target verification rejects the inconsistent prefix. High marginal accuracy can therefore coexist with short committed spans.

## Principles and Analysis

Candidate selection separates coverage from ranking: a selector cannot recover a token absent from its top-k lattice. LiLiCorr encodes the full lattice with a small bidirectional transformer and assigns incoming/outgoing compatibility vectors. Adjacent pair similarity supplies consistency information without a neural forward for every chosen token. For m positions and k candidates, adjacent scoring has (m−1)k² edges, in addition to lattice encoding. Increasing k buys coverage at quadratic pair-scoring cost.

## Design

Use one target, a trained DFlash backbone, and a correlator head. Encode candidates jointly, compute pair scores in batches, and walk left to right conditioned on previous choices. Keep target causal verification responsible for correctness. A deterministic walk gives point-mass proposals; a sampled walk must report its normalized conditional distributions, not the backbone's unary probabilities. DFlash2 instead uses learned predecessor/successor token embeddings and local convolution; its head weights and geometry are not interchangeable with LiLiCorr.

## Implementation

The core flow is:

1. From the committed prefix and verified anchor, obtain top-k candidates per draft slot.
2. Encode the lattice; produce incoming/outgoing vectors and adjacent compatibility scores.
3. Select or sample a path using unary and compatibility information.
4. Verify that chain causally, apply exact acceptance/correction, and commit only valid outputs.
5. Discard unused paths and rejected suffixes.

The paper co-trains backbone and correlator using oracle-prefix cross-entropy and target-weighted distractor penalties. The cited SGLang guide says no LiLiCorr drafter is published, so this path requires producing compatible weights. Pinned vLLM requires `LiLiCorrDraftModel`, complete geometry/head weights, and Model Runner V2; a trained block of 16 includes an anchor and normally supplies 15 speculative slots. Longer lengths lack learned slot embeddings.

## Impact and Results

Joint selection addresses inconsistent combinations rather than serial draft latency. Its added lattice attention, embeddings, projections, and pair scoring must be paid before target verification; improved coverage alone does not imply a longer accepted prefix. SpecForge's oracle top-k and covered-accuracy metrics are recorded-block training diagnostics, not stochastic serving acceptance. In SGLang the checkpoint selects the architecture under `DFLASH`; host length synchronization and eager head fallback can consume the coherence gain. The vLLM guide marks LiLiCorr-specific block-rejection GPU validation and adaptive-verification composition as pending. Shorter blocks use a prefix of the trained geometry rather than new learned slots.

## References

- [LiLiCorr paper](https://arxiv.org/html/2608.20530): lattice encoding, compatibility vectors, path selection, and training objectives.
- [Pinned vLLM guide](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/lilicorr.md): checkpoint requirements, length conventions, proposal sampling, and pending validation.
- [Pinned SGLang guide](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx): public-weight availability and graph/backend conditions.
- [SpecForge training guide](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md): DFlash2 selector objectives and coverage/ranking diagnostics, distinct from a LiLiCorr training recipe.
