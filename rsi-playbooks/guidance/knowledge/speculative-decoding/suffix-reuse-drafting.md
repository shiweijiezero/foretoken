<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Avoid neural drafting by reusing suffix continuations

English | [简体中文](suffix-reuse-drafting_zh.md)

## Observations and Motivation

Code edits, repeated agent turns, and repeated rollouts often generate continuations already present in a prompt or earlier outputs. Searching those token sequences can avoid a neural draft forward. Unlike prefix caching, suffix speculation predicts unknown output rather than reusing computation for known input.

## Principles and Analysis

Suffix Decoding matches the current trailing tokens against indexed histories, ranks continuations by frequency/path scores, and adapts proposal depth. It uses one target, no auxiliary neural model, and no newly trained weights. Counts estimate likely usefulness, but deterministic selection still has point-mass q. If the target probability of that token is p(x), exact token-wise acceptance is p(x); rejection samples the target distribution excluding x. Only actual frequency sampling uses normalized counts as q.

## Design

The original report maintains a CPU global suffix tree of earlier outputs and a per-request tree of active prompt/generation, with compact candidate trees. The engine adapter determines whether verification receives a tree or a chain. Restrict shared history to permitted data scope and retention rules: exact sampling does not authorize cross-tenant retention. Update indexes only from committed output; rejected candidates and cancelled speculative work are not observations of target generation.

## Implementation

A complete proposal cycle is:

1. Find matches for the committed suffix in request-local and permitted shared indexes.
2. Extract continuations, score paths by continuation frequencies, and cap depth according to match strength and the configured maximum.
3. Return the actual candidate layout and, for sampled proposals, the realized normalized proposal distributions.
4. Run the existing causal target verifier and correction sampler.
5. Commit the accepted path/prefix plus correction/bonus; index those committed tokens and query the new suffix next time.

Pinned vLLM integrates suffix speculation through `arctic-inference`. `num_speculative_tokens` is a maximum, not a fixed per-request length. Prompt n-gram matching is also available. Arctic Inference owns indexing and its installed adapter's layout/lifecycle. [Sampling and correction](correction-fused-sampling.md) explains the point-mass rule.

## Impact and Results

Reuse removes auxiliary model residency, training, and neural draft computation. It adds lookup, extraction, CPU/device transfer, index updates, and target verification. Long repeated spans provide useful proposals; a changed identifier immediately after a match can invalidate most verified rows. Novel prose, high-entropy sampling, and ambiguous continuations reduce useful span. Larger history improves potential coverage but increases memory, update cost, and lookup tail latency; warm shared history and request-local prompt reuse have different available evidence.

## References

- [Suffix Decoding report](https://arxiv.org/html/2411.04975): request/global trees, continuation frequencies, and adaptive expansion (2024).
- [Pinned vLLM suffix guide](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/suffix.md): integration, dependency, and maximum-length semantics.
- [Arctic Inference](https://github.com/snowflakedb/ArcticInference) owns history indexing; use the installed adapter revision to determine its returned proposal layout and index lifecycle.
- [Pinned vLLM sampler](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/sample/rejection_sampler.py): point-mass proposals and residual correction.
