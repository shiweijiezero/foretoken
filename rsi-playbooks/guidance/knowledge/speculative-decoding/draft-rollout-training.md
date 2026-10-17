<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Improve draft rollouts through self-conditioned training

English | [简体中文](draft-rollout-training_zh.md)

## Observations and Motivation

A draft trained only on verified target features encounters different inputs when it feeds back its own hidden outputs several positions ahead. Strong teacher-forced token accuracy can collapse during serving rollouts. Self-conditioned training addresses that mismatch rather than merely enlarging the auxiliary model.

## Principles and Analysis

EAGLE-3 fuses low-, middle-, and high-level target features and predicts tokens without the final-feature regression used by earlier EAGLE. Unverified future positions lack target features, so the draft feeds back its own hidden outputs. Training-time simulation exposes the module to that input distribution. For conditional acceptance a[j], expected accepted chain length is sum_j product_i<=j a[i]; errors compound, making later rollout behavior important even when first-position accuracy is high.

## Design

Separate the proposal sources. A standalone draft is a second token-level model with its own prefix processing. EAGLE-3 is separately trained and target-feature-conditioned, with model-specific embedding/projection reuse. Native MTP uses a supported target's trained prediction layers, often without an independently supplied checkpoint; some assistant paths still require one. Repeatedly invoking one head is not equivalent to several independently trained future-position heads. All paths retain draft/verify/commit ownership: rejected proposals never become the next committed history.

## Implementation

For EAGLE-3 training and serving:

1. Produce target responses and capture the selected feature layers with matching tokenizer, chat template, positions, and loss masks.
2. Fuse target features with token embeddings at clean positions.
3. Unroll the draft on its own hidden outputs and optimize token prediction on those self-conditioned inputs.
4. Export a target-compatible auxiliary checkpoint.
5. At inference, draft a chain/tree, verify causal paths with the target, and rebuild draft history from committed tokens and verified features.

Pinned SpecForge provides `specforge train --config ...` and online/offline EAGLE3 recipes. Offline training reads recorded features; online target inference belongs to a SGLang capture service, not the trainer. Existing vLLM/SGLang paths load matching EAGLE3 or supported MTP weights. A new target without compatible weights requires training.

## Impact and Results

Self-conditioning reduces exposure mismatch at later positions, potentially increasing useful prefix survival without adding a training operation to inference. It still costs response generation, feature capture, training, auxiliary residency, and draft forwards. Recorded features avoid repeated target inference but fix the data distribution; target updates, quantization, or backend changes can alter feature alignment. On-policy responses reflect the target's continuation behavior. Domain coverage and prefix formatting, including BOS/template handling, affect alignment independently of training loss; the cited vLLM preamble fix supplies `--skip-chat-template` for supported benchmark datasets.

## References

- [EAGLE-3 paper](https://arxiv.org/html/2503.01840): multi-layer fusion, removal of feature regression, and training-time feedback.
- [Pinned SpecForge training guide](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/docs/sections/basic_usage/training.md) and its [README](https://github.com/sgl-project/SpecForge/blob/fdcc2eddf1b56b5b24a5a15d3abcb366adfeb5aa/README.md): EAGLE3 capture/training recipes and metrics.
- [Pinned vLLM EAGLE](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/eagle.md) and [MTP guide](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/mtp.md), plus [SGLang source documentation](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/docs/docs/advanced_features/speculative_decoding.mdx).
- [vLLM PR #25916](https://github.com/vllm-project/vllm/pull/25916) is merged, 2025-10-02, commit `1cab2f9cad22807150c93b4b5635ff884884ca5a`; its preamble correction adds `--skip-chat-template` to supported benchmark datasets.
